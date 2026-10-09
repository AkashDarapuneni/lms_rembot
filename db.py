"""Storage: SQLite locally; Postgres or MySQL/TiDB when DATABASE_URL is set.

A remote database is needed on hosts whose disk is wiped on restart (e.g. Render's free plan).
  postgresql://user:pass@host:5432/db   -> Postgres (Supabase, Neon, ...)
  mysql://user:pass@host:4000/db        -> MySQL-compatible (TiDB Cloud, ...)
"""
import sqlite3
from urllib.parse import unquote, urlparse

DEFAULT_OFFSETS = "1d,6h,2h,1h,50m,10m"

# One schema that works on SQLite, Postgres and MySQL (VARCHAR where a default is needed).
TABLES = """
CREATE TABLE IF NOT EXISTS users (
    chat_id BIGINT PRIMARY KEY,
    site_url TEXT, token_enc TEXT,
    tz VARCHAR(64) DEFAULT 'Asia/Kolkata',
    offsets VARCHAR(255) DEFAULT '1d,6h,2h,1h,50m,10m',
    digest_time VARCHAR(8) DEFAULT '08:00',
    last_digest VARCHAR(16) DEFAULT '',
    quiet_start VARCHAR(8), quiet_end VARCHAR(8),
    last_sync BIGINT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    chat_id BIGINT, event_id BIGINT,
    name TEXT, course_id BIGINT, course TEXT,
    due_ts BIGINT, url TEXT, kind VARCHAR(32),
    done INTEGER DEFAULT 0,
    snooze_until BIGINT DEFAULT 0,
    PRIMARY KEY (chat_id, event_id)
);
CREATE TABLE IF NOT EXISTS sent (
    chat_id BIGINT, event_id BIGINT, offset_s BIGINT,
    PRIMARY KEY (chat_id, event_id, offset_s)
);
CREATE TABLE IF NOT EXISTS muted (
    chat_id BIGINT, course_id BIGINT, course TEXT,
    PRIMARY KEY (chat_id, course_id)
);
CREATE TABLE IF NOT EXISTS line_pool (
    stage VARCHAR(8), h VARCHAR(40),
    quote TEXT, note TEXT, gif TEXT,
    source VARCHAR(8), created BIGINT,
    last_used BIGINT DEFAULT 0, uses BIGINT DEFAULT 0,
    PRIMARY KEY (stage, h)
);
CREATE TABLE IF NOT EXISTS stats (
    chat_id BIGINT PRIMARY KEY,
    points BIGINT DEFAULT 0, submitted BIGINT DEFAULT 0, early BIGINT DEFAULT 0,
    streak BIGINT DEFAULT 0, best_streak BIGINT DEFAULT 0, last_day VARCHAR(10) DEFAULT ''
);
CREATE TABLE IF NOT EXISTS media_pool (
    file_unique_id VARCHAR(100) PRIMARY KEY,
    file_id TEXT, kind VARCHAR(12), tag VARCHAR(8),
    added_by BIGINT, created BIGINT,
    last_used BIGINT DEFAULT 0, uses BIGINT DEFAULT 0
);
"""

# Columns added after the first release: (name, definition)
NEW_USER_COLUMNS = [
    ("source", "VARCHAR(8) DEFAULT 'api'"),  # 'api' (web service token) or 'ics' (calendar export URL)
    ("moodle_userid", "BIGINT"),  # LMS user id taken from the calendar link / API
    ("style", "INTEGER DEFAULT 1"),  # 1 = Telugu punchlines in reminders
    ("memes", "INTEGER DEFAULT 1"),  # 1 = send memes / GIFs / stickers with reminders
    ("ai_lines", "INTEGER DEFAULT 1"),  # 1 = also use dialogue lines written by Gemini
]


class DB:
    def __init__(self, target: str):
        self.target = target
        self.pg = target.startswith(("postgres://", "postgresql://"))
        self.my = target.startswith(("mysql://", "mysql+pymysql://"))
        self._conn_errors: tuple = ()
        self._connect()
        self._init_schema()

    # ---- connection ----
    def _connect(self):
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row

            self._conn_errors = (psycopg.OperationalError, psycopg.InterfaceError)
            self.c = psycopg.connect(
                self.target, autocommit=True, row_factory=dict_row, prepare_threshold=None,
                connect_timeout=15, client_encoding="UTF8",
            )
        elif self.my:
            import pymysql
            from pymysql.cursors import DictCursor

            u = urlparse(self.target.replace("mysql+pymysql://", "mysql://", 1))
            self._conn_errors = (pymysql.err.OperationalError, pymysql.err.InterfaceError)
            kwargs = {}
            if u.hostname not in ("localhost", "127.0.0.1"):  # remote (TiDB Cloud) requires TLS
                import certifi

                kwargs = {"ssl_ca": certifi.where(), "ssl_verify_cert": True, "ssl_verify_identity": True}
            self.c = pymysql.connect(
                host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""),
                password=unquote(u.password or ""), database=u.path.lstrip("/"),
                cursorclass=DictCursor, autocommit=True, charset="utf8mb4",
                connect_timeout=15, **kwargs,
            )
        else:
            self.c = sqlite3.connect(self.target, check_same_thread=False)
            self.c.row_factory = sqlite3.Row

    def _run(self, sql, args):
        if self.my:
            cur = self.c.cursor()
            cur.execute(sql, args or None)
            return cur
        return self.c.execute(sql, args)

    def _exec(self, sql, args=()):
        if self.pg or self.my:
            sql = sql.replace("?", "%s")
        try:
            cur = self._run(sql, args)
        except self._conn_errors:  # dropped connection: reconnect once and retry
            self._connect()
            cur = self._run(sql, args)
        if not (self.pg or self.my):
            self.c.commit()
        return cur

    def _upsert(self, table, cols, args, keys, update, extra=""):
        """INSERT or update on key conflict, in the right syntax for the database."""
        base = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
        if self.my:
            sets = ",".join(f"{c}=VALUES({c})" for c in update) + extra
            sql = base + "ON DUPLICATE KEY UPDATE " + sets
        else:
            sets = ",".join(f"{c}=excluded.{c}" for c in update) + extra
            sql = base + f"ON CONFLICT({','.join(keys)}) DO UPDATE SET " + sets
        self._exec(sql, args)

    def _insert_ignore(self, table, cols, args):
        ph = ",".join("?" * len(cols))
        if self.my:
            sql = f"INSERT IGNORE INTO {table}({','.join(cols)}) VALUES({ph})"
        else:
            sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({ph}) ON CONFLICT DO NOTHING"
        self._exec(sql, args)

    def _init_schema(self):
        for stmt in TABLES.split(";"):
            if stmt.strip():
                self._exec(stmt)
        if self.pg:
            for name, definition in NEW_USER_COLUMNS:
                self._exec(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {name} {definition}")
            return
        if self.my:
            rows = self._exec(
                "SELECT column_name AS cn FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name='users'"
            ).fetchall()
            have = {r["cn"].lower() for r in rows}
        else:
            have = {r["name"] for r in self._exec("PRAGMA table_info(users)").fetchall()}
        for name, definition in NEW_USER_COLUMNS:
            if name not in have:
                self._exec(f"ALTER TABLE users ADD COLUMN {name} {definition}")

    # ---- users ----
    def get_user(self, chat_id):
        return self._exec("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()

    def all_users(self):
        return self._exec("SELECT * FROM users WHERE token_enc IS NOT NULL").fetchall()

    def save_login(self, chat_id, site, token_enc, source="api", moodle_userid=None):
        self._upsert(
            "users",
            ["chat_id", "site_url", "token_enc", "source", "moodle_userid"],
            (chat_id, site, token_enc, source, moodle_userid),
            ["chat_id"],
            ["site_url", "token_enc", "source", "moodle_userid"],
            extra=",last_sync=0",
        )

    def set_field(self, chat_id, field, value):
        assert field in {"tz", "offsets", "digest_time", "last_digest", "quiet_start", "quiet_end", "last_sync", "style", "memes", "ai_lines"}
        self._exec(f"UPDATE users SET {field}=? WHERE chat_id=?", (value, chat_id))

    def delete_user(self, chat_id):
        for t in ("users", "events", "sent", "muted", "stats"):
            self._exec(f"DELETE FROM {t} WHERE chat_id=?", (chat_id,))

    # ---- events ----
    def get_event(self, chat_id, event_id):
        return self._exec("SELECT * FROM events WHERE chat_id=? AND event_id=?", (chat_id, event_id)).fetchone()

    def upsert_event(self, chat_id, e):
        self._upsert(
            "events",
            ["chat_id", "event_id", "name", "course_id", "course", "due_ts", "url", "kind"],
            (chat_id, e["event_id"], e["name"], e["course_id"], e["course"], e["due_ts"], e["url"], e["kind"]),
            ["chat_id", "event_id"],
            ["name", "course_id", "course", "due_ts", "url", "kind"],
            extra=",done=0",
        )

    def pending_events(self, chat_id, since=None, until=None, include_muted=False):
        q = "SELECT * FROM events WHERE chat_id=? AND done=0"
        args = [chat_id]
        if since is not None:
            q += " AND due_ts>=?"
            args.append(since)
        if until is not None:
            q += " AND due_ts<?"
            args.append(until)
        if not include_muted:
            q += " AND course_id NOT IN (SELECT course_id FROM muted WHERE chat_id=?)"
            args.append(chat_id)
        q += " ORDER BY due_ts"
        return self._exec(q, args).fetchall()

    def mark_done(self, chat_id, event_id):
        self._exec("UPDATE events SET done=1 WHERE chat_id=? AND event_id=?", (chat_id, event_id))

    def set_snooze(self, chat_id, event_id, until):
        self._exec("UPDATE events SET snooze_until=? WHERE chat_id=? AND event_id=?", (until, chat_id, event_id))

    # ---- sent markers ----
    def sent_offsets(self, chat_id, event_id):
        rows = self._exec("SELECT offset_s FROM sent WHERE chat_id=? AND event_id=?", (chat_id, event_id)).fetchall()
        return {r["offset_s"] for r in rows}

    def mark_sent(self, chat_id, event_id, offsets):
        for o in offsets:
            if self.my:
                sql = "INSERT IGNORE INTO sent(chat_id,event_id,offset_s) VALUES(?,?,?)"
            else:
                sql = "INSERT INTO sent(chat_id,event_id,offset_s) VALUES(?,?,?) ON CONFLICT DO NOTHING"
            self._exec(sql, (chat_id, event_id, o))

    def clear_sent(self, chat_id, event_id):
        self._exec("DELETE FROM sent WHERE chat_id=? AND event_id=?", (chat_id, event_id))

    # ---- muted courses ----
    def mute_course(self, chat_id, course_id, course):
        self._upsert("muted", ["chat_id", "course_id", "course"], (chat_id, course_id, course),
                     ["chat_id", "course_id"], ["course"])

    def unmute_course(self, chat_id, course_id):
        self._exec("DELETE FROM muted WHERE chat_id=? AND course_id=?", (chat_id, course_id))

    def muted_courses(self, chat_id):
        return self._exec("SELECT * FROM muted WHERE chat_id=?", (chat_id,)).fetchall()

    def is_muted(self, chat_id, course_id):
        return self._exec("SELECT 1 FROM muted WHERE chat_id=? AND course_id=?", (chat_id, course_id)).fetchone() is not None

    # ---- dialogue pool (each line is used at most once per reuse window, see punchlines.py) ----
    def pool_add(self, stage, h, quote, note, gif, source, ts):
        cols = ["stage", "h", "quote", "note", "gif", "source", "created"]
        args = (stage, h, quote, note, gif, source, ts)
        if source == "fixed":  # lines from punchlines.json: keep them in sync with the file
            self._upsert("line_pool", cols, args, ["stage", "h"], ["quote", "note", "gif", "source"])
        else:
            self._insert_ignore("line_pool", cols, args)

    def pool_counts(self, cutoff):
        """{stage: (lines free to use, total lines)}; a line is free if it was last used before `cutoff`."""
        rows = self._exec(
            "SELECT stage, COUNT(*) AS total, SUM(CASE WHEN last_used<? THEN 1 ELSE 0 END) AS free "
            "FROM line_pool GROUP BY stage", (cutoff,),
        ).fetchall()
        return {r["stage"]: (int(r["free"] or 0), int(r["total"])) for r in rows}

    def pool_free(self, stage, cutoff, limit=200, source=None):
        q, args = "SELECT * FROM line_pool WHERE stage=? AND last_used<?", [stage, cutoff]
        if source:
            q += " AND source=?"
            args.append(source)
        return self._exec(q + f" LIMIT {int(limit)}", args).fetchall()

    def pool_oldest(self, stage, source=None):
        q, args = "SELECT * FROM line_pool WHERE stage=?", [stage]
        if source:
            q += " AND source=?"
            args.append(source)
        return self._exec(q + " ORDER BY last_used LIMIT 1", args).fetchone()

    def pool_touch(self, stage, h, ts):
        self._exec("UPDATE line_pool SET last_used=?, uses=uses+1 WHERE stage=? AND h=?", (ts, stage, h))

    def pool_quotes(self, stage):
        return [r["quote"] for r in self._exec("SELECT quote FROM line_pool WHERE stage=?", (stage,)).fetchall()]

    def pool_fixed_hashes(self, stage):
        rows = self._exec("SELECT h FROM line_pool WHERE stage=? AND source='fixed'", (stage,)).fetchall()
        return {r["h"] for r in rows}

    def pool_delete(self, stage, h):
        self._exec("DELETE FROM line_pool WHERE stage=? AND h=?", (stage, h))

    # ---- stats: points, level, streak ----
    def get_stats(self, chat_id):
        row = self._exec("SELECT * FROM stats WHERE chat_id=?", (chat_id,)).fetchone()
        if row:
            return dict(row)
        return {"chat_id": chat_id, "points": 0, "submitted": 0, "early": 0, "streak": 0, "best_streak": 0, "last_day": ""}

    def record_submit(self, chat_id, points, early, day, yesterday):
        s = self.get_stats(chat_id)
        if s["last_day"] == day:
            streak = s["streak"] or 1
        elif s["last_day"] == yesterday:
            streak = s["streak"] + 1
        else:
            streak = 1
        cols = ["chat_id", "points", "submitted", "early", "streak", "best_streak", "last_day"]
        self._upsert(
            "stats", cols,
            (chat_id, s["points"] + points, s["submitted"] + 1, s["early"] + (1 if early else 0),
             streak, max(s["best_streak"], streak), day),
            ["chat_id"], cols[1:],
        )
        return self.get_stats(chat_id)

    def count_users(self):
        return int(self._exec("SELECT COUNT(*) AS n FROM users WHERE token_enc IS NOT NULL").fetchone()["n"])

    # ---- memes / GIFs / stickers taught by the admin (Telegram file ids) ----
    def media_add(self, uid, file_id, kind, tag, added_by, ts):
        cols = ["file_unique_id", "file_id", "kind", "tag", "added_by", "created"]
        self._upsert("media_pool", cols, (uid, file_id, kind, tag, added_by, ts), ["file_unique_id"], ["file_id", "kind", "tag"])

    def media_free(self, tags, cutoff, limit=100):
        ph = ",".join("?" * len(tags))
        return self._exec(
            f"SELECT * FROM media_pool WHERE tag IN ({ph}) AND last_used<? LIMIT {int(limit)}", (*tags, cutoff)
        ).fetchall()

    def media_oldest(self, tags):
        ph = ",".join("?" * len(tags))
        return self._exec(f"SELECT * FROM media_pool WHERE tag IN ({ph}) ORDER BY last_used LIMIT 1", tuple(tags)).fetchone()

    def media_touch(self, uid, ts):
        self._exec("UPDATE media_pool SET last_used=?, uses=uses+1 WHERE file_unique_id=?", (ts, uid))

    def media_counts(self):
        return self._exec("SELECT tag, kind, COUNT(*) AS n FROM media_pool GROUP BY tag, kind").fetchall()
