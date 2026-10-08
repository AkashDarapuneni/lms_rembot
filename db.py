"""Storage: SQLite locally, Postgres when DATABASE_URL is set.

Postgres is needed on hosts whose disk is wiped on restart (e.g. Render's free plan).
"""
import sqlite3

DEFAULT_OFFSETS = "1d,6h,2h,1h,50m,10m"

TABLES = """
CREATE TABLE IF NOT EXISTS users (
    chat_id BIGINT PRIMARY KEY,
    site_url TEXT, token_enc TEXT,
    tz TEXT DEFAULT 'Asia/Kolkata',
    offsets TEXT DEFAULT '1d,6h,2h,1h,50m,10m',
    digest_time TEXT DEFAULT '08:00',
    last_digest TEXT DEFAULT '',
    quiet_start TEXT, quiet_end TEXT,
    last_sync BIGINT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    chat_id BIGINT, event_id BIGINT,
    name TEXT, course_id BIGINT, course TEXT,
    due_ts BIGINT, url TEXT, kind TEXT,
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
"""

# Columns added after the first release: (name, definition)
NEW_USER_COLUMNS = [
    ("source", "TEXT DEFAULT 'api'"),  # 'api' (web service token) or 'ics' (calendar export URL)
    ("moodle_userid", "BIGINT"),  # LMS user id taken from the calendar link / API
    ("style", "INTEGER DEFAULT 1"),  # 1 = Telugu punchlines in reminders
]


class DB:
    def __init__(self, target: str):
        self.target = target
        self.pg = target.startswith(("postgres://", "postgresql://"))
        self._conn_errors: tuple = ()
        self._connect()
        self._init_schema()

    def _connect(self):
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row

            self._conn_errors = (psycopg.OperationalError, psycopg.InterfaceError)
            self.c = psycopg.connect(
                self.target, autocommit=True, row_factory=dict_row, prepare_threshold=None,
                connect_timeout=15, client_encoding="UTF8",
            )
        else:
            self.c = sqlite3.connect(self.target, check_same_thread=False)
            self.c.row_factory = sqlite3.Row

    def _exec(self, sql, args=()):
        if self.pg:
            sql = sql.replace("?", "%s")
        try:
            cur = self.c.execute(sql, args)
        except self._conn_errors:  # dropped connection: reconnect once and retry
            self._connect()
            cur = self.c.execute(sql, args)
        if not self.pg:
            self.c.commit()
        return cur

    def _init_schema(self):
        for stmt in TABLES.split(";"):
            if stmt.strip():
                self._exec(stmt)
        if self.pg:
            for name, definition in NEW_USER_COLUMNS:
                self._exec(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {name} {definition}")
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
        self._exec(
            "INSERT INTO users(chat_id, site_url, token_enc, source, moodle_userid) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET site_url=excluded.site_url, "
            "token_enc=excluded.token_enc, source=excluded.source, "
            "moodle_userid=excluded.moodle_userid, last_sync=0",
            (chat_id, site, token_enc, source, moodle_userid),
        )

    def set_field(self, chat_id, field, value):
        assert field in {"tz", "offsets", "digest_time", "last_digest", "quiet_start", "quiet_end", "last_sync", "style"}
        self._exec(f"UPDATE users SET {field}=? WHERE chat_id=?", (value, chat_id))

    def delete_user(self, chat_id):
        for t in ("users", "events", "sent", "muted"):
            self._exec(f"DELETE FROM {t} WHERE chat_id=?", (chat_id,))

    # ---- events ----
    def get_event(self, chat_id, event_id):
        return self._exec("SELECT * FROM events WHERE chat_id=? AND event_id=?", (chat_id, event_id)).fetchone()

    def upsert_event(self, chat_id, e):
        self._exec(
            "INSERT INTO events(chat_id,event_id,name,course_id,course,due_ts,url,kind) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id,event_id) DO UPDATE SET name=excluded.name, course_id=excluded.course_id, "
            "course=excluded.course, due_ts=excluded.due_ts, url=excluded.url, kind=excluded.kind, done=0",
            (chat_id, e["event_id"], e["name"], e["course_id"], e["course"], e["due_ts"], e["url"], e["kind"]),
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
            self._exec(
                "INSERT INTO sent(chat_id,event_id,offset_s) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                (chat_id, event_id, o),
            )

    def clear_sent(self, chat_id, event_id):
        self._exec("DELETE FROM sent WHERE chat_id=? AND event_id=?", (chat_id, event_id))

    # ---- muted courses ----
    def mute_course(self, chat_id, course_id, course):
        self._exec(
            "INSERT INTO muted(chat_id,course_id,course) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,course_id) DO UPDATE SET course=excluded.course",
            (chat_id, course_id, course),
        )

    def unmute_course(self, chat_id, course_id):
        self._exec("DELETE FROM muted WHERE chat_id=? AND course_id=?", (chat_id, course_id))

    def muted_courses(self, chat_id):
        return self._exec("SELECT * FROM muted WHERE chat_id=?", (chat_id,)).fetchall()

    def is_muted(self, chat_id, course_id):
        return self._exec("SELECT 1 FROM muted WHERE chat_id=? AND course_id=?", (chat_id, course_id)).fetchone() is not None
