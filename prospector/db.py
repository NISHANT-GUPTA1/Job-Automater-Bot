"""Prospector tracker: companies, contacts, emails, Apollo credit use, small settings store."""
import sqlite3
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apollo_id TEXT UNIQUE, name TEXT, domain TEXT UNIQUE, website TEXT, linkedin TEXT,
    size_stage INTEGER, source TEXT,           -- apollo / you
    roles_json TEXT,                           -- open relevant roles found (title, url)
    status TEXT DEFAULT 'new',                 -- new, people_found, no_people, contacted, replied, skipped
    notes TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER, apollo_person_id TEXT UNIQUE,
    name TEXT, title TEXT, tier INTEGER,
    email TEXT, email_status TEXT,
    status TEXT DEFAULT 'found',               -- found, revealed, no_email, sent, followed_up, replied, bounced, skipped
    thread_id TEXT, subject TEXT, sent_at TEXT, followup_at TEXT, replied_at TEXT, reply_snippet TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER, kind TEXT,             -- initial / followup
    subject TEXT, body TEXT,
    status TEXT DEFAULT 'draft',               -- draft, approved, sent, dropped, failed
    batch_date TEXT, tg_message_id INTEGER, message_id TEXT, error TEXT,
    created_at TEXT, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS apollo_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, endpoint TEXT, credits INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS li_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER, contact_id INTEGER,    -- contact_id: the person we emailed (empty for YC founders)
    name TEXT, title TEXT, slug TEXT UNIQUE, profile_url TEXT, source TEXT,
    status TEXT DEFAULT 'queued',              -- queued, invited, accepted, msg_draft, msg_approved, messaged,
                                               -- replied, failed, msg_failed, skipped
    note TEXT, message TEXT, error TEXT, reply_snippet TEXT,
    invited_at TEXT, accepted_at TEXT, messaged_at TEXT, replied_at TEXT, created_at TEXT
);
"""

_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript(SCHEMA)
if "employees" not in [r[1] for r in _conn.execute("PRAGMA table_info(companies)")]:
    _conn.execute("ALTER TABLE companies ADD COLUMN employees INTEGER")
    _conn.execute("ALTER TABLE companies ADD COLUMN description TEXT")
if "attachment" not in [r[1] for r in _conn.execute("PRAGMA table_info(messages)")]:
    _conn.execute("ALTER TABLE messages ADD COLUMN attachment TEXT")   # resume sent with this email
    _conn.execute("ALTER TABLE messages ADD COLUMN attach_note TEXT")  # "reused from job #12" / "new, $0.013"
if "attachment" not in [r[1] for r in _conn.execute("PRAGMA table_info(li_people)")]:
    _conn.execute("ALTER TABLE li_people ADD COLUMN attachment TEXT")  # resume PDF sent with the first message
_conn.commit()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def q(sql: str, params=()) -> list[sqlite3.Row]:
    return _conn.execute(sql, params).fetchall()


def q1(sql: str, params=()):
    return _conn.execute(sql, params).fetchone()


def run(sql: str, params=()) -> int:
    cur = _conn.execute(sql, params)
    _conn.commit()
    return cur.lastrowid


def update(table: str, row_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))


def get_setting(key: str, default: str = "") -> str:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    run("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def credits_this_month(prefix: str = "/") -> int:
    """Credits used this month; prefix "/" = Apollo endpoints, "hunter:search" / "hunter:verify" = Hunter."""
    month = datetime.now().strftime("%Y-%m")
    return q1("SELECT COALESCE(SUM(credits),0) c FROM apollo_usage WHERE created_at LIKE ? AND endpoint LIKE ?",
              (month + "%", prefix + "%"))["c"]


def credits_today(prefix: str = "/") -> int:
    return q1("SELECT COALESCE(SUM(credits),0) c FROM apollo_usage WHERE created_at LIKE ? AND endpoint LIKE ?",
              (datetime.now().strftime("%Y-%m-%d") + "%", prefix + "%"))["c"]


def log_credits(endpoint: str, credits: int) -> None:
    if credits:
        run("INSERT INTO apollo_usage(endpoint, credits, created_at) VALUES(?,?,?)", (endpoint, credits, now()))
