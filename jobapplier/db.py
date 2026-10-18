"""SQLite tracker: jobs, site accounts, emails, resumes, learned answers, LLM spend."""
import sqlite3
from datetime import datetime

from . import config, crypto

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE,
    title TEXT, company TEXT, location TEXT,
    description TEXT,          -- full job description text as scraped
    details_json TEXT,         -- structured analysis (skills, salary, deadline...)
    summary TEXT,
    contact_email TEXT,
    match_score INTEGER,
    status TEXT DEFAULT 'new', -- new, analyzed, applying, needs_input, applied, emailed, failed, skipped, interview, offer, rejected
    resume_path TEXT,
    applied_at TEXT, emailed_at TEXT,
    notes TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT UNIQUE,
    email TEXT, password_enc TEXT,
    login_url TEXT, job_id INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER, to_addr TEXT, subject TEXT, body TEXT, attachment TEXT,
    status TEXT,               -- draft, sent, failed, cancelled
    error TEXT, sent_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER, path TEXT, content_json TEXT,
    status TEXT,               -- draft, approved, rejected
    feedback TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS qa_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT, question_norm TEXT UNIQUE, answer TEXT,
    times_used INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER, kind TEXT, detail TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT, model TEXT, purpose TEXT, job_id INTEGER,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read INTEGER, cache_write INTEGER, cost_usd REAL,
    created_at TEXT
);
"""

_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript(SCHEMA)
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


# ---- jobs -----------------------------------------------------------------

def add_job(url: str, description: str = "") -> tuple[int, bool]:
    """Returns (job_id, created). An already-tracked URL returns the existing id."""
    row = q1("SELECT id FROM jobs WHERE url=?", (url,))
    if row:
        return row["id"], False
    jid = run("INSERT INTO jobs(url, description, status, created_at, updated_at) VALUES(?,?,?,?,?)",
              (url, description, "new", now(), now()))
    log(jid, "added", url)
    return jid, True


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def set_status(job_id: int, status: str, detail: str = "") -> None:
    extra = {}
    if status == "applied":
        extra["applied_at"] = now()
    if status == "emailed":
        extra["emailed_at"] = now()
    update_job(job_id, status=status, **extra)
    log(job_id, f"status:{status}", detail)


def get_job(job_id: int):
    return q1("SELECT * FROM jobs WHERE id=?", (job_id,))


def log(job_id: int | None, kind: str, detail: str = "") -> None:
    run("INSERT INTO events(job_id, kind, detail, created_at) VALUES(?,?,?,?)",
        (job_id, kind, detail[:2000], now()))


# ---- accounts -------------------------------------------------------------

def get_account(domain: str) -> dict | None:
    row = q1("SELECT * FROM accounts WHERE domain=?", (domain,))
    if not row:
        return None
    return {"domain": row["domain"], "email": row["email"],
            "password": crypto.decrypt(row["password_enc"]), "login_url": row["login_url"]}


def save_account(domain: str, email: str, password: str, login_url: str = "", job_id: int | None = None) -> None:
    run("""INSERT INTO accounts(domain, email, password_enc, login_url, job_id, created_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(domain) DO UPDATE SET email=excluded.email, password_enc=excluded.password_enc,
           login_url=excluded.login_url""",
        (domain, email, crypto.encrypt(password), login_url, job_id, now()))


def delete_account(domain: str) -> None:
    run("DELETE FROM accounts WHERE domain=?", (domain,))


def list_accounts() -> list[dict]:
    return [{"domain": r["domain"], "email": r["email"], "password": crypto.decrypt(r["password_enc"]),
             "login_url": r["login_url"], "created_at": r["created_at"]}
            for r in q("SELECT * FROM accounts ORDER BY created_at DESC")]


# ---- emails / resumes -----------------------------------------------------

def add_email(job_id, to_addr, subject, body, attachment, status="draft") -> int:
    return run("""INSERT INTO emails(job_id, to_addr, subject, body, attachment, status, created_at)
                  VALUES(?,?,?,?,?,?,?)""", (job_id, to_addr, subject, body, attachment, status, now()))


def update_email(email_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE emails SET {cols} WHERE id=?", (*fields.values(), email_id))


def add_resume(job_id, path, content_json, status="draft", feedback="") -> int:
    return run("""INSERT INTO resumes(job_id, path, content_json, status, feedback, created_at)
                  VALUES(?,?,?,?,?,?)""", (job_id, path, content_json, status, feedback, now()))


# ---- llm usage ------------------------------------------------------------

def log_usage(provider, model, purpose, job_id, inp, out, cache_read=0, cache_write=0, cost=0.0) -> None:
    run("""INSERT INTO llm_usage(provider, model, purpose, job_id, input_tokens, output_tokens,
           cache_read, cache_write, cost_usd, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (provider, model, purpose, job_id, inp, out, cache_read, cache_write, cost, now()))
