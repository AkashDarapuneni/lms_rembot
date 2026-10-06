"""SQLite storage."""
import sqlite3
import time

DEFAULT_OFFSETS = "1d,6h,2h,1h,50m,10m"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    site_url TEXT, token_enc TEXT,
    tz TEXT DEFAULT 'Asia/Kolkata',
    offsets TEXT DEFAULT '1d,6h,2h,1h,50m,10m',
    digest_time TEXT DEFAULT '08:00',
    last_digest TEXT DEFAULT '',
    quiet_start TEXT, quiet_end TEXT,
    last_sync INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    chat_id INTEGER, event_id INTEGER,
    name TEXT, course_id INTEGER, course TEXT,
    due_ts INTEGER, url TEXT, kind TEXT,
    done INTEGER DEFAULT 0,
    snooze_until INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, event_id)
);
CREATE TABLE IF NOT EXISTS sent (
    chat_id INTEGER, event_id INTEGER, offset_s INTEGER,
    PRIMARY KEY (chat_id, event_id, offset_s)
);
CREATE TABLE IF NOT EXISTS muted (
    chat_id INTEGER, course_id INTEGER, course TEXT,
    PRIMARY KEY (chat_id, course_id)
);
"""


class DB:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(users)")}
        if "source" not in cols:  # 'api' (web service token) or 'ics' (calendar export URL)
            self.c.execute("ALTER TABLE users ADD COLUMN source TEXT DEFAULT 'api'")
            self.c.commit()
        if "moodle_userid" not in cols:  # LMS user id taken from the calendar link / API
            self.c.execute("ALTER TABLE users ADD COLUMN moodle_userid INTEGER")
            self.c.commit()
        if "style" not in cols:  # 1 = funny Telugu punchlines in reminders
            self.c.execute("ALTER TABLE users ADD COLUMN style INTEGER DEFAULT 1")
            self.c.commit()

    # ---- users ----
    def get_user(self, chat_id):
        return self.c.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()

    def all_users(self):
        return self.c.execute("SELECT * FROM users WHERE token_enc IS NOT NULL").fetchall()

    def save_login(self, chat_id, site, token_enc, source="api", moodle_userid=None):
        self.c.execute(
            "INSERT INTO users(chat_id, site_url, token_enc, source, moodle_userid) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET site_url=excluded.site_url, "
            "token_enc=excluded.token_enc, source=excluded.source, "
            "moodle_userid=excluded.moodle_userid, last_sync=0",
            (chat_id, site, token_enc, source, moodle_userid),
        )
        self.c.commit()

    def set_field(self, chat_id, field, value):
        assert field in {"tz", "offsets", "digest_time", "last_digest", "quiet_start", "quiet_end", "last_sync", "style"}
        self.c.execute(f"UPDATE users SET {field}=? WHERE chat_id=?", (value, chat_id))
        self.c.commit()

    def delete_user(self, chat_id):
        for t in ("users", "events", "sent", "muted"):
            self.c.execute(f"DELETE FROM {t} WHERE chat_id=?", (chat_id,))
        self.c.commit()

    # ---- events ----
    def get_event(self, chat_id, event_id):
        return self.c.execute(
            "SELECT * FROM events WHERE chat_id=? AND event_id=?", (chat_id, event_id)
        ).fetchone()

    def upsert_event(self, chat_id, e):
        self.c.execute(
            "INSERT INTO events(chat_id,event_id,name,course_id,course,due_ts,url,kind) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id,event_id) DO UPDATE SET name=excluded.name, course_id=excluded.course_id, "
            "course=excluded.course, due_ts=excluded.due_ts, url=excluded.url, kind=excluded.kind, done=0",
            (chat_id, e["event_id"], e["name"], e["course_id"], e["course"], e["due_ts"], e["url"], e["kind"]),
        )
        self.c.commit()

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
        return self.c.execute(q, args).fetchall()

    def mark_done(self, chat_id, event_id):
        self.c.execute("UPDATE events SET done=1 WHERE chat_id=? AND event_id=?", (chat_id, event_id))
        self.c.commit()

    def set_snooze(self, chat_id, event_id, until):
        self.c.execute(
            "UPDATE events SET snooze_until=? WHERE chat_id=? AND event_id=?", (until, chat_id, event_id)
        )
        self.c.commit()

    # ---- sent markers ----
    def sent_offsets(self, chat_id, event_id):
        rows = self.c.execute(
            "SELECT offset_s FROM sent WHERE chat_id=? AND event_id=?", (chat_id, event_id)
        ).fetchall()
        return {r["offset_s"] for r in rows}

    def mark_sent(self, chat_id, event_id, offsets):
        self.c.executemany(
            "INSERT OR IGNORE INTO sent VALUES(?,?,?)", [(chat_id, event_id, o) for o in offsets]
        )
        self.c.commit()

    def clear_sent(self, chat_id, event_id):
        self.c.execute("DELETE FROM sent WHERE chat_id=? AND event_id=?", (chat_id, event_id))
        self.c.commit()

    # ---- muted courses ----
    def mute_course(self, chat_id, course_id, course):
        self.c.execute("INSERT OR REPLACE INTO muted VALUES(?,?,?)", (chat_id, course_id, course))
        self.c.commit()

    def unmute_course(self, chat_id, course_id):
        self.c.execute("DELETE FROM muted WHERE chat_id=? AND course_id=?", (chat_id, course_id))
        self.c.commit()

    def muted_courses(self, chat_id):
        return self.c.execute("SELECT * FROM muted WHERE chat_id=?", (chat_id,)).fetchall()

    def is_muted(self, chat_id, course_id):
        return (
            self.c.execute(
                "SELECT 1 FROM muted WHERE chat_id=? AND course_id=?", (chat_id, course_id)
            ).fetchone()
            is not None
        )
