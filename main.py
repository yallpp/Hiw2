#!/usr/bin/env python3
"""
HI2 CHECKER BOT — Premium Edition v94
Multi-user · Owner Approval · Credit System · TXT Upload · Admin Panel
Daily Bonus · Force Join Tasks · Premium Plans · Referral · Redeem Codes
HI2 Mail Pool · Database Backup · Leaderboard · Proxy Manager
+ Smart Proxy Scoring · Error Taxonomy · Blacklist · Result Filters
+ Stronger concurrency · Live owner dashboard
Payment flow kept manual (contact owner) as requested.
"""

import asyncio
import io
import itertools
import logging
import os
import random
import re
import shutil
import sqlite3
import string
import tempfile
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from threading import Lock

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

_raw_token = os.environ.get("BOT_TOKEN", "8645260113:AAFYU0s9dt_V4UuNEifeDm_-7zDGNtB3gGA").strip()
if not _raw_token:
    raise SystemExit("FATAL: BOT_TOKEN is required.")
BOT_TOKEN = _raw_token

_raw_owner = os.environ.get("OWNER_ID", "8609127164").strip()
if not _raw_owner:
    raise SystemExit("FATAL: OWNER_ID is required.")
try:
    OWNER_ID = int(_raw_owner)
except ValueError:
    raise SystemExit("FATAL: OWNER_ID must be an integer.")

MAX_UPLOAD_MB        = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "2"))
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "1"))
EMAILS_CONCURRENCY   = int(os.environ.get("EMAILS_CONCURRENCY",   "25"))  # v94 fast: 25 workers
BOT_BOOT_TIME        = time.time()                                       # for uptime display
LOG_LEVEL            = os.environ.get("LOG_LEVEL", "INFO")
OWNER_USERNAME       = os.environ.get("OWNER_USERNAME", "").lstrip("@")
BACKUP_DIR           = os.environ.get(
    "BACKUP_DIR",
    os.path.join(tempfile.gettempdir(), "hi2_backups")
)

# ── Retry / cooldown tuning (v94 FAST — speed + fewer errors) ────────────────
# More workers, shorter timeouts, tighter backoff, extra attempt.
# Captcha is the bottleneck — we rotate proxy immediately on captcha fail
# and keep backoff low so bulk stays fast.
MAX_CHECK_ATTEMPTS   = int(os.environ.get("MAX_CHECK_ATTEMPTS",   "4"))
MAX_CAPTCHA_ATTEMPTS = int(os.environ.get("MAX_CAPTCHA_ATTEMPTS", "2"))
PROXY_COOLDOWN_SECS  = int(os.environ.get("PROXY_COOLDOWN_SECS",  "90"))   # revive faster
RETRY_DELAY_SECS     = float(os.environ.get("RETRY_DELAY_SECS",   "0.25")) # was 0.5
HI2_API_TIMEOUT      = int(os.environ.get("HI2_API_TIMEOUT",      "9"))   # was 12
CAPTCHA_TIMEOUT      = int(os.environ.get("CAPTCHA_TIMEOUT",      "6"))   # was 8
# End-of-task retry pass: re-check every 'error' email one more time
# with fresh proxies + direct fallback. Set to 0 to disable.
RETRY_ERRORS_AT_END  = int(os.environ.get("RETRY_ERRORS_AT_END",  "1"))
# Fast mode: skip long sleep on pure captcha fails (proxy rotate is enough)
FAST_CAPTCHA_ROTATE  = os.environ.get("FAST_CAPTCHA_ROTATE", "1") == "1"


def _backoff_delay(attempt: int) -> float:
    """v94 tight backoff: 0.25 → 0.5 → 1 → 2 → 4 (capped 6s)."""
    return min(RETRY_DELAY_SECS * (2 ** (attempt - 1)), 6.0)

ALLOWED_DOMAINS = ("hi2.in", "telegmail.com")
_SITEKEY = "6LfEUPkgAAAAAKTgbMoewQkWBEQhO2VPL4QviKct"
_CO      = "aHR0cHM6Ly9oaTIuaW46NDQz"
_UA      = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Free recaptcha solver caches (no Capsolver API key needed)
_ver_lock  = Lock()
_ver_cache = {"version": None, "ts": 0.0}
_tok_lock  = Lock()
_tok_cache = {"token": None, "ts": 0.0}

STATUS_EMOJI = {
    "available":        "✅",
    "taken":            "❌",
    "error":            "⚠️",
    "rate_limited":     "🕐",
    "invalid":          "🚫",
    "duplicate":        "🔁",
    "skipped":          "⏭️",
    "captcha_error":    "🧩",
    "timeout":          "⌛",
    "connection_error": "🔌",
    "api_error":        "📡",
    "blacklisted":      "🛑",
}

HEADER = "⚡ 𝗛𝗜𝟮 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 ⚡"
DIV    = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
SUB    = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"

PLAN_DAYS = {"Basic": 7, "Standard": 30, "Pro": 90}


def progress_bar(done: int, total: int, width: int = 14) -> str:
    """Cool progress bar: ███████░░░░░ 58%"""
    if total <= 0:
        return "░" * width + " 0%"
    filled = min(width, max(0, round(width * done / total)))
    pct    = int(done * 100 / total)
    return "█" * filled + "░" * (width - filled) + f" {pct}%"


def mini_bar(value: int, max_value: int, width: int = 12) -> str:
    """Small inline ratio bar: ▓▓▓▓▓░░░░░░░"""
    if max_value <= 0:
        return "░" * width
    filled = min(width, max(0, round(width * value / max_value)))
    return "▓" * filled + "░" * (width - filled)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
)
log = logging.getLogger("hi2bot")

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE CONTEXT — SQLite (default) + PostgreSQL (Render DATABASE_URL)
# ══════════════════════════════════════════════════════════════════════════════

_DB_URL = os.environ.get("DATABASE_URL", "").strip()
USING_PG = _DB_URL.lower().startswith(("postgres://", "postgresql://"))
_DB_PATH: str = ""
_db_lock = Lock()


def _is_connection_url(v: str) -> bool:
    return "://" in v or v.lower().startswith(("postgres:", "postgresql:"))


def _try_path(p: str) -> str | None:
    if not p or p == ":memory:":
        return None
    try:
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "a"):
            pass
        return p
    except OSError as e:
        log.warning("DB path not usable (%s): %s", p, e)
        return None


def _get_db_path() -> str:
    for key in ("SQLITE_DB_PATH", "DB_PATH"):
        c = os.environ.get(key, "").strip()
        if c and not _is_connection_url(c):
            r = _try_path(c)
            if r:
                log.info("DB path from %s: %s", key, r)
                return r
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url and not _is_connection_url(db_url):
        r = _try_path(db_url)
        if r:
            return r
    for p in (
        os.path.join(tempfile.gettempdir(), "hi2checker.db"),  # /tmp — always works on Render
        os.path.expanduser("~/hi2checker.db"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "hi2checker.db"),
    ):
        r = _try_path(p)
        if r:
            log.info("DB path: %s", r)
            return r
    log.error("No writable path — falling back to in-memory. ALL DATA LOST ON RESTART.")
    return ":memory:"


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE CONTEXT
# ══════════════════════════════════════════════════════════════════════════════

def _pg_translate(sql: str) -> str:
    """sqlite-style SQL → PostgreSQL (?→%s, datetime('now')→now())."""
    s = sql.replace("?", "%s")
    s = s.replace("datetime('now')", "now()").replace('datetime("now")', "now()")
    return s


class _PGCursor:
    """sqlite3-style cursor shim over psycopg2 (dict rows, ? placeholders)."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(_pg_translate(sql), tuple(params))
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class _PGConn:
    """sqlite3-style connection shim over psycopg2."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        c = _PGCursor(cur)
        c.execute(sql, params)
        return c

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _pg_connect():
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError:
        raise SystemExit(
            "FATAL: DATABASE_URL is PostgreSQL but psycopg2 is missing.\n"
            "       Install: pip install psycopg2-binary"
        )
    url = _DB_URL
    if url.lower().startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


@contextmanager
def get_db():
    global _DB_PATH
    if USING_PG:
        conn = _pg_connect()
        try:
            yield _PGConn(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return
    if not _DB_PATH:
        _DB_PATH = _get_db_path()
    try:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30)
    except sqlite3.OperationalError:
        _DB_PATH = ":memory:"
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        pass
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _safe_alter(db, table, column, col_def):
    """Add column if missing. On Postgres must rollback after failed ALTER
    or the rest of the transaction dies with InFailedSqlTransaction."""
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        if USING_PG:
            try:
                db.commit()
            except Exception:
                pass
    except Exception:
        if USING_PG:
            try:
                db.rollback()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  INIT DB
# ══════════════════════════════════════════════════════════════════════════════

# ── Master schema (SQLite + PostgreSQL compatible) ──────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    username      TEXT,
    full_name     TEXT,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    credits       INTEGER NOT NULL DEFAULT 0,
    total_checks  INTEGER NOT NULL DEFAULT 0,
    registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS credit_transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(telegram_id),
    amount     INTEGER NOT NULL,
    reason     TEXT,
    actor_id   INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(telegram_id),
    filename     TEXT,
    total        INTEGER NOT NULL DEFAULT 0,
    processed    INTEGER NOT NULL DEFAULT 0,
    available    INTEGER NOT NULL DEFAULT 0,
    taken        INTEGER NOT NULL DEFAULT 0,
    errors       INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    credits_used INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'RUNNING',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT
);
CREATE TABLE IF NOT EXISTS task_results (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    email   TEXT NOT NULL,
    status  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    target_id  INTEGER,
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO settings(key, value) VALUES ('checking_enabled',    '1')  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('maintenance_mode',    '0')  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('referral_reward',     '25') ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('daily_bonus_enabled', '1')  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('daily_bonus_amount',  '10') ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('stay_days',           '2')  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('penalty_amount',      '25') ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('fj_gate_enabled',     '1')  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('streak_days',         '5')  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('streak_bonus',        '50') ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('spin_enabled',        '1')  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('spin_min',            '5')  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('spin_max',            '50') ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('weekly_top_enabled',  '1')  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('auto_backup_enabled', '0')  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('custom_start_text',   '')   ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('vip_silver',          '100') ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('vip_gold',            '300') ON CONFLICT(key) DO NOTHING;
INSERT INTO settings(key, value) VALUES ('vip_diamond',         '600') ON CONFLICT(key) DO NOTHING;

CREATE TABLE IF NOT EXISTS daily_bonus (
    user_id      INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    last_claimed TEXT NOT NULL DEFAULT '',
    streak       INTEGER NOT NULL DEFAULT 0,
    best_streak  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS premium (
    user_id      INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    plan         TEXT NOT NULL,
    activated_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL,
    granted_by   INTEGER
);
CREATE TABLE IF NOT EXISTS referrals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id  INTEGER NOT NULL REFERENCES users(telegram_id),
    referee_id   INTEGER NOT NULL REFERENCES users(telegram_id),
    reward_given INTEGER NOT NULL DEFAULT 0,
    state        TEXT,
    ref_joined_at TEXT,
    penalty      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(referee_id)
);
CREATE TABLE IF NOT EXISTS redeem_codes (
    code          TEXT PRIMARY KEY,
    reward_type   TEXT NOT NULL DEFAULT 'credits',
    reward_amount INTEGER NOT NULL DEFAULT 1,
    max_uses      INTEGER NOT NULL DEFAULT 1,
    uses          INTEGER NOT NULL DEFAULT 0,
    expires_at    TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS redemptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    redeemed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(code, user_id)
);
CREATE TABLE IF NOT EXISTS force_join_tasks (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    reward_credits INTEGER NOT NULL DEFAULT 1,
    reward_mail    INTEGER NOT NULL DEFAULT 0,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS force_join_channels (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL REFERENCES force_join_tasks(id) ON DELETE CASCADE,
    channel_id       TEXT NOT NULL,
    channel_username TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_completions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(telegram_id),
    task_id      TEXT NOT NULL REFERENCES force_join_tasks(id),
    completed_at TEXT NOT NULL DEFAULT (datetime('now')),
    mail_id      INTEGER,
    UNIQUE(user_id, task_id)
);
CREATE TABLE IF NOT EXISTS hi2_mail (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    address       TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'available',
    assigned_to   INTEGER,
    assigned_at   TEXT,
    completion_id INTEGER
);
CREATE TABLE IF NOT EXISTS db_backups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    filename   TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    result     TEXT NOT NULL DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS proxies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_url     TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'untested',
    last_tested   TEXT,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    score         REAL    NOT NULL DEFAULT 0.0
);
CREATE TABLE IF NOT EXISTS blacklist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern    TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL DEFAULT 'email',  -- email | domain | prefix
    reason     TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── v4: Rewards upgrade (force-join gate, spin, panel admins) ──
CREATE TABLE IF NOT EXISTS fj_gate_channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT UNIQUE NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    is_active  INTEGER NOT NULL DEFAULT 1,
    added_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS lucky_spins (
    user_id   INTEGER PRIMARY KEY,
    last_spin TEXT NOT NULL DEFAULT '',
    spins     INTEGER NOT NULL DEFAULT 0,
    total_won INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS withdraw_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    method     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT,
    decided_by INTEGER
);
CREATE TABLE IF NOT EXISTS panel_admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',
    created_by    INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login    TEXT
);
"""


def _pg_ddl(script: str) -> str:
    """DDL transforms for PostgreSQL (SERIAL, now())."""
    s = script.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    s = s.replace("DEFAULT (datetime('now'))", "DEFAULT now()")
    s = s.replace("datetime('now')", "now()")
    return s


def init_db():
    """
    v94.1 — Postgres-safe init.
    Each DDL/DML runs in its own try so one bad statement cannot abort
    the whole transaction (InFailedSqlTransaction on Render).
    """
    def _run(db, sql, params=()):
        try:
            db.execute(sql, params)
            if USING_PG:
                try:
                    db.commit()
                except Exception:
                    pass
            return True
        except Exception as e:
            log.warning("init_db step skipped: %s | %s", e, sql[:80])
            if USING_PG:
                try:
                    db.rollback()
                except Exception:
                    pass
            return False

    with get_db() as db:
        if USING_PG:
            for stmt in _pg_ddl(_SCHEMA).split(";"):
                s = stmt.strip()
                if s:
                    _run(db, s + ";")
        else:
            try:
                db.executescript(_SCHEMA)
            except Exception as e:
                log.warning("sqlite schema script note: %s", e)

        _safe_alter(db, "users", "referral_code",      "TEXT")
        _safe_alter(db, "users", "premium_expires_at", "TEXT")
        # v4 rewards columns
        _safe_alter(db, "users",       "channel_ok",     "INTEGER NOT NULL DEFAULT 0")
        _safe_alter(db, "referrals",   "state",          "TEXT")
        _safe_alter(db, "referrals",   "ref_joined_at",  "TEXT")
        _safe_alter(db, "referrals",   "penalty",        "INTEGER NOT NULL DEFAULT 0")
        _safe_alter(db, "daily_bonus", "streak",         "INTEGER NOT NULL DEFAULT 0")
        _safe_alter(db, "daily_bonus", "best_streak",    "INTEGER NOT NULL DEFAULT 0")
        # v94 proxy scoring + blacklist
        _safe_alter(db, "proxies", "success_count", "INTEGER NOT NULL DEFAULT 0")
        _safe_alter(db, "proxies", "fail_count",    "INTEGER NOT NULL DEFAULT 0")
        _safe_alter(db, "proxies", "score",         "REAL NOT NULL DEFAULT 0.0")

        # legacy referrals (instant-reward era) = already earned/locked
        _run(db, "UPDATE referrals SET state='LOCKED' WHERE state IS NULL")

        # v4 default migration: 1→10 daily, 2→25 referral
        _run(db, "UPDATE settings SET value='10' WHERE key='daily_bonus_amount' AND value='1'")
        _run(db, "UPDATE settings SET value='25' WHERE key='referral_reward' AND value='2'")

        # backfill referral codes for existing users
        try:
            rows = db.execute(
                "SELECT telegram_id FROM users WHERE referral_code IS NULL"
            ).fetchall()
            for r in rows:
                _run(
                    db,
                    "UPDATE users SET referral_code=? WHERE telegram_id=?",
                    (str(uuid.uuid4())[:8], r["telegram_id"]),
                )
        except Exception as e:
            log.warning("referral_code backfill skipped: %s", e)

    log.info("Database ready at: %s%s",
             "postgres" if USING_PG else _DB_PATH,
             "" if not USING_PG else f" ({_DB_URL.split('@')[-1] if '@' in _DB_URL else ''})")


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPERS — USERS
# ══════════════════════════════════════════════════════════════════════════════

def register_user(user) -> bool:
    """Insert user if new. Returns True if newly created."""
    ref_code = str(uuid.uuid4())[:8]
    with _db_lock:
        with get_db() as db:
            existing = db.execute(
                "SELECT telegram_id FROM users WHERE telegram_id=?", (user.id,)
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE users SET username=?, full_name=? WHERE telegram_id=?",
                    (user.username, user.full_name, user.id),
                )
                return False
            # Owner always lands APPROVED — never stuck in pending
            status = "APPROVED" if user.id == OWNER_ID else "PENDING"
            db.execute(
                """INSERT INTO users(telegram_id, username, full_name, status, referral_code)
                   VALUES(?,?,?,?,?)""",
                (user.id, user.username, user.full_name, status, ref_code),
            )
            return True


def get_user(telegram_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
        ).fetchone()


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def apply_referral(referee_id: int, referral_code: str) -> bool:
    """Attach referral on registration — NO instant reward.
    Referral 'PENDING' banta hai; reward tab milta hai jab referee
    `stay_days` (default 2) din channel me rahe (referral_lock_sweep).
    Returns True if attached."""
    with _db_lock:
        with get_db() as db:
            referrer = db.execute(
                "SELECT telegram_id FROM users WHERE referral_code=?", (referral_code,)
            ).fetchone()
            if not referrer or referrer["telegram_id"] == referee_id:  # self-ref block
                return False
            existing = db.execute(
                "SELECT id FROM referrals WHERE referee_id=?", (referee_id,)
            ).fetchone()
            if existing:
                return False
            db.execute(
                """INSERT INTO referrals(referrer_id, referee_id, reward_given, state)
                   VALUES(?,?,0,'PENDING')""",
                (referrer["telegram_id"], referee_id),
            )
            return True


def get_referral_info(user_id: int) -> dict:
    with get_db() as db:
        user = db.execute(
            "SELECT referral_code FROM users WHERE telegram_id=?", (user_id,)
        ).fetchone()
        count = db.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=?", (user_id,)
        ).fetchone()["c"]
        total_earned = db.execute(
            "SELECT COALESCE(SUM(reward_given),0) AS t FROM referrals WHERE referrer_id=?",
            (user_id,),
        ).fetchone()["t"]
        reward_per = db.execute(
            "SELECT value FROM settings WHERE key='referral_reward'"
        ).fetchone()["value"]
        st = lambda s: db.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=? "
            "AND COALESCE(state,'LOCKED')=?", (user_id, s)
        ).fetchone()["c"]
    return {
        "code": user["referral_code"] if user else "",
        "count": count,
        "total_earned": total_earned,
        "reward_per": reward_per,
        "locked": st("LOCKED"),
        "pending": st("PENDING"),
        "penalized": st("PENALIZED"),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPERS — CREDITS
# ══════════════════════════════════════════════════════════════════════════════

def add_credits(user_id: int, amount: int, reason: str, actor_id: int = 0):
    with _db_lock:
        with get_db() as db:
            db.execute(
                "UPDATE users SET credits=credits+? WHERE telegram_id=?", (amount, user_id)
            )
            db.execute(
                "INSERT INTO credit_transactions(user_id,amount,reason,actor_id) VALUES(?,?,?,?)",
                (user_id, amount, reason, actor_id),
            )


def deduct_one_credit(user_id: int) -> bool:
    with _db_lock:
        with get_db() as db:
            row = db.execute(
                "SELECT credits FROM users WHERE telegram_id=?", (user_id,)
            ).fetchone()
            if not row or row["credits"] < 1:
                return False
            db.execute(
                "UPDATE users SET credits=credits-1 WHERE telegram_id=?", (user_id,)
            )
            db.execute(
                "INSERT INTO credit_transactions(user_id,amount,reason,actor_id) VALUES(?,?,'check_deduct',0)",
                (user_id, -1),
            )
            return True


def refund_one_credit(user_id: int):
    with _db_lock:
        with get_db() as db:
            db.execute(
                "UPDATE users SET credits=credits+1 WHERE telegram_id=?", (user_id,)
            )
            db.execute(
                "INSERT INTO credit_transactions(user_id,amount,reason,actor_id) VALUES(?,?,'check_refund',0)",
                (user_id, 1),
            )


def remove_credits(user_id: int, amount: int, actor_id: int) -> bool:
    with _db_lock:
        with get_db() as db:
            row = db.execute(
                "SELECT credits FROM users WHERE telegram_id=?", (user_id,)
            ).fetchone()
            if not row:
                return False
            actual = min(amount, row["credits"])
            if actual <= 0:
                return False
            db.execute(
                "UPDATE users SET credits=credits-? WHERE telegram_id=?", (actual, user_id)
            )
            db.execute(
                "INSERT INTO credit_transactions(user_id,amount,reason,actor_id) VALUES(?,?,?,?)",
                (user_id, -actual, "admin_remove", actor_id),
            )
            return True


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPERS — SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

def get_setting(key: str, default: str = "0") -> str:
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_db() as db:
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def toggle_setting(key: str) -> str:
    current = get_setting(key, "0")
    new = "0" if current == "1" else "1"
    set_setting(key, new)
    return new


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPERS — DAILY BONUS
# ══════════════════════════════════════════════════════════════════════════════

def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def claim_daily_bonus(user_id: int) -> tuple[bool, str]:
    """Returns (success, message). Streak-aware: har claim streak badhata hai;
    har `streak_days` (default 5) lagataar claim pe `streak_bonus` extra milta hai."""
    if get_setting("daily_bonus_enabled", "1") != "1":
        return False, "❌ Daily bonus is currently disabled."
    today = today_utc()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    amount = int(get_setting("daily_bonus_amount", "10"))
    streak_days = int(get_setting("streak_days", "5"))
    streak_bonus = int(get_setting("streak_bonus", "50"))
    with _db_lock:
        with get_db() as db:
            row = db.execute(
                "SELECT last_claimed, streak, best_streak FROM daily_bonus WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if row and row["last_claimed"] == today:
                s = row["streak"] or 0
                return False, (
                    "⏰ Already claimed today. Come back tomorrow (UTC).\n"
                    f"🔥 Current streak: *{s} day{'s' if s != 1 else ''}* "
                    f"(best: {row['best_streak'] or s})"
                )
            new_streak = (row["streak"] + 1) if (row and row["last_claimed"] == yesterday) else 1
            best = max(new_streak, row["best_streak"] if row else 0)
            db.execute(
                """INSERT INTO daily_bonus(user_id, last_claimed, streak, best_streak)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     last_claimed=excluded.last_claimed,
                     streak=excluded.streak,
                     best_streak=excluded.best_streak""",
                (user_id, today, new_streak, best),
            )
            extra = streak_bonus if (streak_days > 0 and new_streak % streak_days == 0) else 0
            total = amount + extra
            db.execute(
                "UPDATE users SET credits=credits+? WHERE telegram_id=?",
                (total, user_id),
            )
            db.execute(
                "INSERT INTO credit_transactions(user_id,amount,reason,actor_id) VALUES(?,?,?,0)",
                (user_id, amount, "daily_bonus"),
            )
            if extra:
                db.execute(
                    "INSERT INTO credit_transactions(user_id,amount,reason,actor_id) VALUES(?,?,?,0)",
                    (user_id, extra, "streak_bonus"),
                )
    msg = f"✅ +{amount} credit claimed!\n🔥 Streak: *{new_streak} day{'s' if new_streak != 1 else ''}*"
    if extra:
        msg += f"\n🎉 *STREAK BONUS! +{extra}* ({streak_days}-day streak)"
    return True, msg


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPERS — PREMIUM
# ══════════════════════════════════════════════════════════════════════════════

def is_premium(user_id: int) -> bool:
    with get_db() as db:
        row = db.execute(
            "SELECT expires_at FROM premium WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return False
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return exp > datetime.now(timezone.utc)
        except Exception:
            return False


def grant_premium(user_id: int, plan: str, days: int, grantor_id: int):
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with _db_lock:
        with get_db() as db:
            db.execute(
                """INSERT INTO premium(user_id, plan, expires_at, granted_by)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       plan=excluded.plan,
                       activated_at=datetime('now'),
                       expires_at=excluded.expires_at,
                       granted_by=excluded.granted_by""",
                (user_id, plan, expires, grantor_id),
            )
            db.execute(
                "UPDATE users SET premium_expires_at=? WHERE telegram_id=?",
                (expires, user_id),
            )


def revoke_premium(user_id: int):
    with _db_lock:
        with get_db() as db:
            db.execute("DELETE FROM premium WHERE user_id=?", (user_id,))
            db.execute(
                "UPDATE users SET premium_expires_at=NULL WHERE telegram_id=?", (user_id,)
            )


def get_premium_info(user_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM premium WHERE user_id=?", (user_id,)
        ).fetchone()


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPERS — REDEEM CODES
# ══════════════════════════════════════════════════════════════════════════════

def create_redeem_code(
    code: str,
    reward_type: str,
    reward_amount: int,
    max_uses: int,
    expires_at: str | None,
) -> bool:
    try:
        with _db_lock:
            with get_db() as db:
                db.execute(
                    """INSERT INTO redeem_codes(code,reward_type,reward_amount,max_uses,expires_at)
                       VALUES(?,?,?,?,?)""",
                    (code.upper(), reward_type, reward_amount, max_uses, expires_at),
                )
        return True
    except sqlite3.IntegrityError:
        return False


def claim_redeem_code(user_id: int, code: str) -> tuple[bool, str]:
    code = code.strip().upper()
    with _db_lock:
        with get_db() as db:
            rc = db.execute(
                "SELECT * FROM redeem_codes WHERE code=?", (code,)
            ).fetchone()
            if not rc:
                return False, "❌ Invalid code."
            if not rc["enabled"]:
                return False, "❌ Code is disabled."
            if rc["uses"] >= rc["max_uses"]:
                return False, "❌ Code has reached its usage limit."
            if rc["expires_at"]:
                try:
                    exp = datetime.fromisoformat(rc["expires_at"])
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if exp < datetime.now(timezone.utc):
                        return False, "❌ Code has expired."
                except Exception:
                    pass
            existing = db.execute(
                "SELECT id FROM redemptions WHERE code=? AND user_id=?",
                (code, user_id),
            ).fetchone()
            if existing:
                return False, "❌ You have already redeemed this code."

            # All checks passed — apply reward
            db.execute(
                "INSERT INTO redemptions(code, user_id) VALUES(?,?)", (code, user_id)
            )
            db.execute(
                "UPDATE redeem_codes SET uses=uses+1 WHERE code=?", (code,)
            )
            amt = rc["reward_amount"]
            rtype = rc["reward_type"]

            if rtype == "credits":
                db.execute(
                    "UPDATE users SET credits=credits+? WHERE telegram_id=?",
                    (amt, user_id),
                )
                db.execute(
                    "INSERT INTO credit_transactions(user_id,amount,reason,actor_id) VALUES(?,?,'redeem_code',0)",
                    (user_id, amt),
                )
                return True, f"✅ Redeemed! +{amt} credits added."
            elif rtype == "premium_days":
                from datetime import timedelta
                plan = "Redeem"
                expires = (datetime.now(timezone.utc) + timedelta(days=amt)).isoformat()
                db.execute(
                    """INSERT INTO premium(user_id,plan,expires_at,granted_by) VALUES(?,?,?,0)
                       ON CONFLICT(user_id) DO UPDATE SET
                           plan=excluded.plan,
                           activated_at=datetime('now'),
                           expires_at=excluded.expires_at""",
                    (user_id, plan, expires),
                )
                db.execute(
                    "UPDATE users SET premium_expires_at=? WHERE telegram_id=?",
                    (expires, user_id),
                )
                return True, f"✅ Redeemed! Premium activated for {amt} days."
            else:
                return False, "❌ Unknown reward type."


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPERS — FORCE JOIN TASKS
# ══════════════════════════════════════════════════════════════════════════════

def get_next_available_task(user_id: int):
    with get_db() as db:
        completed = {
            r["task_id"]
            for r in db.execute(
                "SELECT task_id FROM task_completions WHERE user_id=?", (user_id,)
            ).fetchall()
        }
        tasks = db.execute(
            "SELECT * FROM force_join_tasks WHERE enabled=1 ORDER BY created_at"
        ).fetchall()
        for t in tasks:
            if t["id"] not in completed:
                return t
    return None


def get_task_channels(task_id: str):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM force_join_channels WHERE task_id=?", (task_id,)
        ).fetchall()


def complete_task(user_id: int, task_id: str) -> tuple[bool, int, str | None]:
    """
    Mark task complete. Awards credits and optionally assigns mail.
    Returns (success, credits_awarded, mail_address_or_None).
    """
    with _db_lock:
        with get_db() as db:
            # Guard against double-complete
            exists = db.execute(
                "SELECT id FROM task_completions WHERE user_id=? AND task_id=?",
                (user_id, task_id),
            ).fetchone()
            if exists:
                return False, 0, None

            task = db.execute(
                "SELECT * FROM force_join_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not task:
                return False, 0, None

            credits = task["reward_credits"]
            mail_addr = None
            mail_id = None

            # Assign mail if enabled
            if task["reward_mail"]:
                mail = db.execute(
                    "SELECT id, address FROM hi2_mail WHERE status='available' LIMIT 1"
                ).fetchone()
                if mail:
                    mail_id = mail["id"]
                    mail_addr = mail["address"]
                    db.execute(
                        """UPDATE hi2_mail SET status='assigned', assigned_to=?,
                           assigned_at=datetime('now') WHERE id=?""",
                        (user_id, mail_id),
                    )

            # Record completion
            db.execute(
                "INSERT INTO task_completions(user_id, task_id, mail_id) VALUES(?,?,?)",
                (user_id, task_id, mail_id),
            )

            # Update mail completion_id
            if mail_id:
                last_id = db.execute(
                    "SELECT id FROM task_completions WHERE user_id=? AND task_id=?",
                    (user_id, task_id),
                ).fetchone()["id"]
                db.execute(
                    "UPDATE hi2_mail SET completion_id=? WHERE id=?", (last_id, mail_id)
                )

            # Award credits
            db.execute(
                "UPDATE users SET credits=credits+? WHERE telegram_id=?",
                (credits, user_id),
            )
            db.execute(
                "INSERT INTO credit_transactions(user_id,amount,reason,actor_id) VALUES(?,?,'task_complete',0)",
                (user_id, credits),
            )

    return True, credits, mail_addr


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPERS — HI2 MAIL
# ══════════════════════════════════════════════════════════════════════════════

def add_mail(address: str) -> bool:
    try:
        with _db_lock:
            with get_db() as db:
                db.execute(
                    "INSERT INTO hi2_mail(address) VALUES(?)", (address.strip(),)
                )
        return True
    except sqlite3.IntegrityError:
        return False


def get_mail_stats() -> dict:
    with get_db() as db:
        total    = db.execute("SELECT COUNT(*) AS c FROM hi2_mail").fetchone()["c"]
        avail    = db.execute("SELECT COUNT(*) AS c FROM hi2_mail WHERE status='available'").fetchone()["c"]
        assigned = total - avail
    return {"total": total, "available": avail, "assigned": assigned}


def delete_mail(mail_id: int) -> bool:
    with _db_lock:
        with get_db() as db:
            row = db.execute(
                "SELECT id FROM hi2_mail WHERE id=? AND status='available'", (mail_id,)
            ).fetchone()
            if not row:
                return False
            db.execute("DELETE FROM hi2_mail WHERE id=?", (mail_id,))
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPERS — AUDIT & STATS
# ══════════════════════════════════════════════════════════════════════════════

def log_audit(actor_id: int, action: str, target_id: int = 0, detail: str = ""):
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO audit_logs(actor_id,action,target_id,detail) VALUES(?,?,?,?)",
                (actor_id, action, target_id, detail),
            )
    except Exception as e:
        log.warning("audit_log error: %s", e)


def get_leaderboard(limit: int = 10):
    with get_db() as db:
        return db.execute(
            """SELECT telegram_id, full_name, username, total_checks, credits
               FROM users WHERE status='APPROVED'
               ORDER BY total_checks DESC, credits DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


# ══════════════════════════════════════════════════════════════════════════════
#  PROXY MANAGER
# ══════════════════════════════════════════════════════════════════════════════
#
#  Format accepted:  host:port  OR  user:pass@host:port  (http/https/socks5)
#  If no scheme given, http:// is assumed.
#
#  Round-robin: one proxy used per check at a time (_proxy_lock prevents
#  two calls from grabbing the same proxy simultaneously).
#  Dead proxy → auto-marked, skipped until re-tested.

_proxy_lock  = Lock()
_proxy_cycle = None          # itertools.cycle rebuilt after each DB change
_proxy_cycle_lock = Lock()


def _build_proxy_url(raw: str) -> str:
    """Normalise raw proxy string to a full URL."""
    raw = raw.strip()
    if "://" not in raw:
        raw = "http://" + raw
    return raw


def proxy_add(proxy_url: str) -> bool:
    url = _build_proxy_url(proxy_url)
    try:
        with _db_lock:
            with get_db() as db:
                db.execute(
                    "INSERT INTO proxies(proxy_url) VALUES(?)", (url,)
                )
        _rebuild_proxy_cycle()
        return True
    except sqlite3.IntegrityError:
        return False


def proxy_delete(proxy_id: int) -> bool:
    with _db_lock:
        with get_db() as db:
            row = db.execute("SELECT id FROM proxies WHERE id=?", (proxy_id,)).fetchone()
            if not row:
                return False
            db.execute("DELETE FROM proxies WHERE id=?", (proxy_id,))
    _rebuild_proxy_cycle()
    return True


def proxy_list():
    with get_db() as db:
        return db.execute(
            "SELECT * FROM proxies ORDER BY id"
        ).fetchall()


def proxy_set_status(proxy_id: int, status: str):
    with _db_lock:
        with get_db() as db:
            db.execute(
                "UPDATE proxies SET status=?, last_tested=datetime('now') WHERE id=?",
                (status, proxy_id),
            )


def _test_one_proxy(proxy_id: int, proxy_url: str) -> str:
    """
    v3.3 — Test proxy *connectivity*, not the full hi2.in flow.
    
    Previously this tested GET https://hi2.in — but hi2.in blocks plain GET
    requests (returns 403), so every proxy was marked 'dead' even though
    they worked fine for the actual POST API calls.
    
    Now tests against Google's reCAPTCHA endpoint (the same endpoint used
    for captcha solving in production). This verifies:
      1. Proxy can connect
      2. HTTPS works
      3. Google is reachable (needed for captcha solve)
    
    Returns 'live' or 'dead'. A 'live' proxy can reach Google — whether
    captcha actually solves depends on Google's bot-detection mood, not
    the proxy, so we don't test that here.
    """
    test_urls = [
        "https://www.google.com/recaptcha/api.js",   # primary — same as production
        "https://www.gstatic.com/recaptcha/releases/",  # fallback
    ]
    status = "dead"
    try:
        s = requests.Session()
        s.proxies = {"http": proxy_url, "https": proxy_url}
        s.headers.update({"User-Agent": _UA, "Accept": "*/*"})
        for url in test_urls:
            try:
                r = s.get(url, timeout=HI2_API_TIMEOUT, allow_redirects=True)
                # Any 2xx/3xx/4xx means proxy CONNECTS fine.
                # Only 5xx or timeout/error means proxy is broken.
                if r.status_code < 500:
                    status = "live"
                    break
            except requests.exceptions.Timeout:
                continue  # try next URL
            except Exception:
                continue  # try next URL
    except Exception as e:
        log.debug("Proxy #%s test failed entirely: %s", proxy_id, e)
        status = "dead"
    proxy_set_status(proxy_id, status)
    _rebuild_proxy_cycle()
    return status


def _test_one_proxy_full_flow(proxy_id: int, proxy_url: str) -> dict:
    """
    v3.3 — Full-flow test: captcha solve + hi2.in API call.
    Returns dict with:
      {'connectivity': 'live'/'dead',
       'captcha': 'ok'/'fail',
       'api': 'ok'/'fail'/'blocked',
       'overall': 'live'/'partial'/'dead'}
    
    'partial' = proxy connects but captcha or API has issues — still
    usable for retry (the actual _check_email will retry on failure).
    """
    result = {
        "connectivity": "dead",
        "captcha": "fail",
        "api": "fail",
        "overall": "dead",
    }
    
    # 1. Connectivity test
    try:
        s = requests.Session()
        s.proxies = {"http": proxy_url, "https": proxy_url}
        s.headers.update({"User-Agent": _UA})
        r = s.get("https://www.google.com/recaptcha/api.js",
                  timeout=HI2_API_TIMEOUT, allow_redirects=True)
        if r.status_code < 500:
            result["connectivity"] = "live"
    except Exception:
        result["connectivity"] = "dead"
        result["overall"] = "dead"
        proxy_set_status(proxy_id, "dead")
        _rebuild_proxy_cycle()
        return result
    
    # 2. Captcha test (same as production)
    token = _get_token_via_proxy(proxy_url)
    result["captcha"] = "ok" if token else "fail"
    
    # 3. hi2.in reachability test (just check we can connect, not full API)
    try:
        r2 = s.get("https://hi2.in/", timeout=HI2_API_TIMEOUT, allow_redirects=True)
        # hi2.in returns 200/403/200 for GET — any response means reachable
        result["api"] = "ok" if r2.status_code < 500 else "blocked"
    except Exception:
        result["api"] = "fail"
    
    # Overall verdict
    if result["connectivity"] == "live" and result["captcha"] == "ok":
        result["overall"] = "live"
        proxy_set_status(proxy_id, "live")
    elif result["connectivity"] == "live":
        # Proxy connects but captcha fails — keep as 'untested' so it stays
        # in the cycle and gets retried during real checks
        result["overall"] = "partial"
        proxy_set_status(proxy_id, "untested")
    else:
        result["overall"] = "dead"
        proxy_set_status(proxy_id, "dead")
    _rebuild_proxy_cycle()
    return result


def _rebuild_proxy_cycle():
    """
    v94 — Score-ordered cycle.
    Prefer high-score live proxies, then untested. Dead stay out.
    Score = success / (success + fail + 1) * 100, updated on every check.
    """
    global _proxy_cycle
    with get_db() as db:
        usable = db.execute(
            """SELECT id, proxy_url, COALESCE(score, 0) AS score
               FROM proxies
               WHERE status IN ('live','untested')
               ORDER BY score DESC, id ASC"""
        ).fetchall()
    with _proxy_cycle_lock:
        if usable:
            _proxy_cycle = itertools.cycle([(r["id"], r["proxy_url"]) for r in usable])
        else:
            _proxy_cycle = None


def _get_next_proxy():
    """Return (proxy_id, proxy_url) or (None, None) if no usable proxy."""
    with _proxy_cycle_lock:
        if _proxy_cycle is None:
            return None, None
        return next(_proxy_cycle)


def _proxy_record_result(proxy_id: int, success: bool):
    """Update success/fail counters and recompute score for a proxy."""
    if not proxy_id:
        return
    with _db_lock:
        with get_db() as db:
            if success:
                db.execute(
                    "UPDATE proxies SET success_count = success_count + 1, "
                    "last_tested = datetime('now') WHERE id=?",
                    (proxy_id,),
                )
            else:
                db.execute(
                    "UPDATE proxies SET fail_count = fail_count + 1, "
                    "last_tested = datetime('now') WHERE id=?",
                    (proxy_id,),
                )
            row = db.execute(
                "SELECT success_count, fail_count FROM proxies WHERE id=?",
                (proxy_id,),
            ).fetchone()
            if row:
                s = row["success_count"] or 0
                f = row["fail_count"] or 0
                score = (s / (s + f + 1)) * 100.0
                db.execute("UPDATE proxies SET score=? WHERE id=?", (score, proxy_id))


# ─────────────────────────────────────────────────────────────────────────────
#  PROXY COOLDOWN / AUTO-REVIVE  (v3.1)
# ─────────────────────────────────────────────────────────────────────────────
# Previously: one failure → proxy permanently marked 'dead' until manual retest.
# Now: failures mark 'dead' but auto-revive after PROXY_COOLDOWN_SECS (default 5 min).
# Combined with retry logic in _check_email, this drops error rate from ~50% to ~10%.

_revive_lock = Lock()
_last_revive_check = 0.0


def _revive_old_dead_proxies(force: bool = False) -> int:
    """
    Auto-revive proxies whose 'dead' status is older than PROXY_COOLDOWN_SECS.
    Called opportunistically (max once every 30s) from _check_email.
    Returns count revived.
    """
    global _last_revive_check
    now_ts = time.time()
    if not force and now_ts - _last_revive_check < 30:
        return 0
    _last_revive_check = now_ts

    from datetime import timedelta
    cutoff_dt = datetime.now(timezone.utc) - timedelta(seconds=PROXY_COOLDOWN_SECS)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    revived = 0
    with _revive_lock:
        with _db_lock:
            with get_db() as db:
                cursor = db.execute(
                    "UPDATE proxies SET status='untested' "
                    "WHERE status='dead' AND last_tested < ?",
                    (cutoff_str,),
                )
                revived = cursor.rowcount
    if revived > 0:
        log.info("♻️ Auto-revived %d dead proxies (cooldown expired)", revived)
        _rebuild_proxy_cycle()
    return revived


def _mark_proxy_cooldown(proxy_id: int, reason: str = "fail"):
    """Mark proxy as 'dead' — will auto-revive after cooldown. Also records fail."""
    _proxy_record_result(proxy_id, success=False)
    proxy_set_status(proxy_id, "dead")
    _rebuild_proxy_cycle()


def _mark_proxy_live(proxy_id: int):
    """Promote an 'untested' proxy to 'live' after a successful check + score bump."""
    with _db_lock:
        with get_db() as db:
            db.execute(
                "UPDATE proxies SET status='live', last_tested=datetime('now') "
                "WHERE id=? AND status='untested'",
                (proxy_id,),
            )
    _proxy_record_result(proxy_id, success=True)


# ─────────────────────────────────────────────────────────────────────────────
#  BULK / ADVANCED PROXY HELPERS  (v3 additions)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_proxy_lines(raw: str) -> list[str]:
    """
    Parse a free-form proxy list into clean normalised URLs.
    Accepts:
      - one proxy per line
      - comma / semicolon / pipe / whitespace separated
      - mixed schemes (http/https/socks5/socks4)
      - user:pass@host:port  or  host:port
      - '://' containing passwords with special chars is preserved
    """
    out: list[str] = []
    seen: set[str] = set()
    # Normalise newlines first
    norm = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Split by newline, comma, semicolon, pipe, tab, whitespace
    tokens = re.split(r"[\n,;|\s]+", norm)
    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        # Quick validation: must contain ':' or '@' and a port
        if ":" not in t and "@" not in t:
            continue
        url = _build_proxy_url(t)
        low = url.lower()
        if low not in seen:
            seen.add(low)
            out.append(url)
    return out


def proxy_add_bulk(raw_list: str) -> dict:
    """
    Add multiple proxies in one shot.
    Returns: {'added': int, 'duplicate': int, 'invalid': int, 'added_urls': list}
    """
    urls = _parse_proxy_lines(raw_list)
    added = 0
    duplicate = 0
    added_urls: list[str] = []
    if not urls:
        return {"added": 0, "duplicate": 0, "invalid": 1, "added_urls": []}

    with _db_lock:
        with get_db() as db:
            existing = {
                r["proxy_url"].lower()
                for r in db.execute("SELECT proxy_url FROM proxies").fetchall()
            }
            for url in urls:
                if url.lower() in existing:
                    duplicate += 1
                    continue
                try:
                    db.execute(
                        "INSERT INTO proxies(proxy_url) VALUES(?)", (url,)
                    )
                    existing.add(url.lower())
                    added += 1
                    added_urls.append(url)
                except sqlite3.IntegrityError:
                    duplicate += 1
    _rebuild_proxy_cycle()
    return {
        "added": added,
        "duplicate": duplicate,
        "invalid": 0,
        "added_urls": added_urls,
    }


def proxy_clear_all() -> int:
    """Delete all proxies. Returns count removed."""
    with _db_lock:
        with get_db() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM proxies").fetchone()["c"]
            db.execute("DELETE FROM proxies")
    _rebuild_proxy_cycle()
    return count


def proxy_clear_dead() -> int:
    """Delete all proxies marked 'dead'. Returns count removed."""
    with _db_lock:
        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM proxies WHERE status='dead'"
            ).fetchone()["c"]
            db.execute("DELETE FROM proxies WHERE status='dead'")
    _rebuild_proxy_cycle()
    return count


def proxy_list_filtered(status: str | None = None):
    """Return proxies optionally filtered by status."""
    with get_db() as db:
        if status and status != "all":
            return db.execute(
                "SELECT * FROM proxies WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        return db.execute(
            "SELECT * FROM proxies ORDER BY id"
        ).fetchall()


def proxy_export(status: str | None = None) -> str:
    """Return a newline-separated proxy list (optionally filtered)."""
    rows = proxy_list_filtered(status)
    return "\n".join(r["proxy_url"] for r in rows)


def proxy_stats() -> dict:
    """Return full proxy stats: live, dead, untested, total + avg score."""
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) AS c FROM proxies").fetchone()["c"]
        live = db.execute(
            "SELECT COUNT(*) AS c FROM proxies WHERE status='live'"
        ).fetchone()["c"]
        dead = db.execute(
            "SELECT COUNT(*) AS c FROM proxies WHERE status='dead'"
        ).fetchone()["c"]
        unt = db.execute(
            "SELECT COUNT(*) AS c FROM proxies WHERE status='untested'"
        ).fetchone()["c"]
        avg = db.execute(
            "SELECT COALESCE(AVG(score),0) AS a FROM proxies WHERE status IN ('live','untested')"
        ).fetchone()["a"]
    return {"total": total, "live": live, "dead": dead, "untested": unt, "avg_score": round(avg or 0, 1)}


async def _test_proxies_concurrent(proxies_rows, concurrency: int = 8) -> dict:
    """
    Test multiple proxies in parallel using a thread pool.
    Returns {'live': int, 'dead': int}.
    """
    loop_ref = asyncio.get_running_loop()
    live_c = 0
    dead_c = 0

    def _do_test(args):
        pid, purl = args
        return _test_one_proxy(pid, purl)

    args_list = [(p["id"], p["proxy_url"]) for p in proxies_rows]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [loop_ref.run_in_executor(pool, _do_test, a) for a in args_list]
        for fut in asyncio.as_completed(futures):
            result = await fut
            if result == "live":
                live_c += 1
            else:
                dead_c += 1
    _rebuild_proxy_cycle()
    return {"live": live_c, "dead": dead_c}



# ══════════════════════════════════════════════════════════════════════════════
#  BLACKLIST  (v94)
# ══════════════════════════════════════════════════════════════════════════════

def blacklist_add(pattern: str, kind: str = "email", reason: str = "", created_by: int = 0) -> bool:
    pattern = pattern.strip().lower()
    if not pattern:
        return False
    try:
        with _db_lock:
            with get_db() as db:
                db.execute(
                    "INSERT INTO blacklist(pattern, kind, reason, created_by) VALUES(?,?,?,?)",
                    (pattern, kind, reason, created_by),
                )
        return True
    except Exception:
        return False


def blacklist_remove(pattern: str) -> bool:
    with _db_lock:
        with get_db() as db:
            cur = db.execute("DELETE FROM blacklist WHERE pattern=?", (pattern.strip().lower(),))
            return cur.rowcount > 0


def blacklist_list(limit: int = 50):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM blacklist ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def is_blacklisted(email: str) -> bool:
    """Check email / domain / prefix against blacklist patterns."""
    email = email.strip().lower()
    if "@" not in email:
        return False
    user, domain = email.split("@", 1)
    with get_db() as db:
        rows = db.execute("SELECT pattern, kind FROM blacklist").fetchall()
    for r in rows:
        pat = (r["pattern"] or "").lower()
        kind = r["kind"] or "email"
        if kind == "email" and pat == email:
            return True
        if kind == "domain" and (pat == domain or domain.endswith("." + pat)):
            return True
        if kind == "prefix" and user.startswith(pat):
            return True
        # plain pattern match fallback
        if kind == "email" and pat in email:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  RECAPTCHA SOLVER  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  FREE RECAPTCHA SOLVER  (no Capsolver API key needed)
# ══════════════════════════════════════════════════════════════════════════════

def _get_version() -> str:
    """Fetch current reCAPTCHA v2 version string from Google (cached 1h)."""
    with _ver_lock:
        if _ver_cache["version"] and time.time() - _ver_cache["ts"] < 3600:
            return _ver_cache["version"]
    try:
        r = requests.get(
            "https://www.google.com/recaptcha/api.js",
            headers={"User-Agent": _UA},
            timeout=8,
        )
        m = re.search(r"/releases/([\w-]+)/", r.text)
        if m:
            with _ver_lock:
                _ver_cache["version"] = m.group(1)
                _ver_cache["ts"] = time.time()
            return m.group(1)
    except Exception:
        pass
    return "rAqPVhe2JMK6mSJKi8r_vw"


def _get_token() -> str | None:
    """Solve reCAPTCHA v2 invisibly via Google's own anchor/reload endpoints (cached 90s)."""
    with _tok_lock:
        if _tok_cache["token"] and time.time() - _tok_cache["ts"] < 90:
            return _tok_cache["token"]

    tok = _get_token_via_proxy(None)
    if tok:
        with _tok_lock:
            _tok_cache["token"] = tok
            _tok_cache["ts"] = time.time()
    return tok


def _solve_captcha_once(proxy_url: str | None, version: str) -> str | None:
    """
    Single attempt at solving reCAPTCHA v2 invisible via Google's anchor/reload.
    Returns the rresp token, or None on failure.
    """
    import random
    cb = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": _UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "*/*",
        "Referer": "https://hi2.in/",
    })
    if proxy_url:
        sess.proxies = {"http": proxy_url, "https": proxy_url}

    anchor_url = (
        f"https://www.google.com/recaptcha/api2/anchor"
        f"?ar=1&k={_SITEKEY}&co={_CO}&hl=en&v={version}&size=invisible&cb={cb}"
    )
    try:
        r1 = sess.get(anchor_url, timeout=CAPTCHA_TIMEOUT)
    except Exception as e:
        log.debug("captcha anchor failed: %s", e)
        return None

    if r1.status_code != 200 or not r1.text:
        return None

    # Multiple regex patterns — Google's response format varies
    challenge = None
    for pat in (
        r'"rresp","([^"]+)"',
        r'id="recaptcha-token" value="([^"]+)"',
        r'name="recaptcha-token" value="([^"]+)"',
        r'token":"([^"]+)"',
    ):
        m = re.search(pat, r1.text)
        if m:
            challenge = m.group(1)
            break
    if not challenge:
        return None

    # Short jitter only — long sleep kills bulk speed (v94 fast)
    time.sleep(0.08 + random.random() * 0.12)

    reload_url = f"https://www.google.com/recaptcha/api2/reload?k={_SITEKEY}"
    payload = (
        f"v={version}&reason=q&c={urllib.parse.quote(challenge)}"
        f"&k={_SITEKEY}&co={_CO}&hl=en&size=invisible"
        f"&chr=%5B89%2C64%2C27%5D&vh=13599012192&bg=!GgA4oQEAABkEBQAAAA"
    )
    try:
        r2 = sess.post(
            reload_url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": anchor_url,
                "Origin": "https://www.google.com",
            },
            timeout=CAPTCHA_TIMEOUT,
        )
    except Exception as e:
        log.debug("captcha reload failed: %s", e)
        return None

    if r2.status_code != 200 or not r2.text:
        return None

    # Multiple patterns for the reload response too
    for pat in (
        r'"rresp","([^"]+)"',
        r'\["rresp","([^"]+)"',
        r'rresp":"([^"]+)"',
        r'"token":"([^"]+)"',
    ):
        m = re.search(pat, r2.text)
        if m and len(m.group(1)) > 20:  # token sanity check
            return m.group(1)
    return None


def _get_token_via_proxy(proxy_url: str | None) -> str | None:
    """
    v3.2 — Solve reCAPTCHA v2 invisibly with internal retry.
    Both anchor and reload go through the SAME proxy as the hi2.in API call,
    so Google and hi2.in see one consistent IP. No shared token cache —
    each proxy gets its own.

    Retries up to 3 times with fresh cb each attempt. Between attempts,
    re-fetches the reCAPTCHA version (in case Google rolled out a new one).
    """
    version = _get_version()

    for captcha_attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
        token = _solve_captcha_once(proxy_url, version)
        if token:
            return token
        if captcha_attempt < MAX_CAPTCHA_ATTEMPTS:
            # v94: tight retry — rotate is more valuable than waiting
            time.sleep(0.15 + (captcha_attempt * 0.1))
            if captcha_attempt >= 1:
                version = _get_version()
    return None


def _invalidate_token():
    with _tok_lock:
        _tok_cache["token"] = None
        _tok_cache["ts"] = 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  HI2 CHECKER  — proxy-aware, free captcha solver
# ══════════════════════════════════════════════════════════════════════════════

def _check_email(email: str) -> str:
    """
    v3.2 — Aggressive retry-aware email checker.
    Tries up to MAX_CHECK_ATTEMPTS times, rotating to a different proxy on
    each attempt and falling back to direct (no proxy) on the final attempt
    if all proxies are exhausted. Captcha is solved fresh per attempt via
    `_get_token_via_proxy` (which itself retries internally up to
    MAX_CAPTCHA_ATTEMPTS times).

    Exponential backoff between attempts: 1s → 2s → 4s → 8s → 16s.

    Failure modes that trigger a retry instead of returning "error":
      • Captcha solve failed (3 internal retries × 5 outer = up to 15 attempts)
      • Proxy connection error / timeout
      • hi2.in returned 429 (rate-limited)
      • hi2.in returned 400 with recaptcha error
      • hi2.in returned unexpected status code
    """
    email = email.strip()
    try:
        parts = email.split("@")
        username = parts[0]
        domain   = parts[1].lower()
    except IndexError:
        return "invalid"
    if domain not in ALLOWED_DOMAINS:
        return "invalid"
    if is_blacklisted(email):
        return "blacklisted"

    # Opportunistically revive dead proxies whose cooldown has expired
    _revive_old_dead_proxies()

    tried_proxy_ids: set[int] = set()
    last_failure = "error"

    for attempt in range(1, MAX_CHECK_ATTEMPTS + 1):
        # Pick a proxy we haven't tried yet in this call, if possible
        proxy_id, proxy_url = _get_next_proxy()
        if proxy_id and proxy_id in tried_proxy_ids:
            # Cycle has cycled back to one we already used — try direct instead
            proxy_id, proxy_url = None, None
        if proxy_id:
            tried_proxy_ids.add(proxy_id)

        # Prefer direct earlier when proxies keep dying on captcha (v94)
        # Last attempt always tries direct if any proxy was already used.
        if proxy_url is not None and len(tried_proxy_ids) >= 1:
            if attempt == MAX_CHECK_ATTEMPTS or (
                attempt >= 2 and last_failure == "captcha_error" and attempt % 2 == 0
            ):
                proxy_id, proxy_url = None, None

        # ── Solve captcha (internal retry inside _get_token_via_proxy) ─────
        # IMPORTANT: same proxy_url is used for BOTH captcha solve AND hi2.in
        # API call below — Google and hi2.in see the same IP. This is required
        # because reCAPTCHA tokens are IP-bound.
        if proxy_url:
            log.debug(
                "Attempt %d/%d for %s via proxy #%s — captcha+API same IP",
                attempt, MAX_CHECK_ATTEMPTS, email, proxy_id,
            )
        token = _get_token_via_proxy(proxy_url)

        if not token:
            # v3.3/v94: Do NOT mark proxy dead on captcha failure.
            # Captcha failure is almost always Google's bot-detection, not a
            # broken proxy. Just retry with a different proxy.
            if proxy_id:
                log.info(
                    "Captcha failed via proxy #%s (attempt %d/%d) — Google blocked, retrying",
                    proxy_id, attempt, MAX_CHECK_ATTEMPTS,
                )
            else:
                log.info(
                    "Captcha failed direct (attempt %d/%d) — retrying",
                    attempt, MAX_CHECK_ATTEMPTS,
                )
            last_failure = "captcha_error"
            if attempt < MAX_CHECK_ATTEMPTS:
                # Fast rotate: almost no sleep on captcha fail — proxy change is the fix
                if FAST_CAPTCHA_ROTATE:
                    time.sleep(0.05)
                else:
                    time.sleep(_backoff_delay(attempt))
            continue

        # ── Call hi2.in API (SAME proxy as captcha — IP consistency!) ──────
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        try:
            r = requests.post(
                "https://hi2.in/api/custom",
                data={"domain": domain, "prefix": username, "recaptcha": token},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin":       "https://hi2.in",
                    "Referer":      "https://hi2.in/",
                    "User-Agent":   _UA,
                },
                proxies=proxies,
                timeout=HI2_API_TIMEOUT,
            )
        except requests.exceptions.Timeout:
            if proxy_id:
                _mark_proxy_cooldown(proxy_id, reason="timeout")
                log.warning(
                    "Proxy #%s timeout (attempt %d/%d) — cooldown",
                    proxy_id, attempt, MAX_CHECK_ATTEMPTS,
                )
            last_failure = "timeout"
            if attempt < MAX_CHECK_ATTEMPTS:
                time.sleep(_backoff_delay(attempt))
            continue
        except Exception as e:
            if proxy_id:
                _mark_proxy_cooldown(proxy_id, reason="conn_err")
                log.warning(
                    "Proxy #%s conn error (%s) attempt %d/%d — cooldown",
                    proxy_id, e, attempt, MAX_CHECK_ATTEMPTS,
                )
            else:
                log.warning("Direct call error (%s): %s", email, e)
            last_failure = "connection_error"
            if attempt < MAX_CHECK_ATTEMPTS:
                time.sleep(_backoff_delay(attempt))
            continue

        # ── Handle response ───────────────────────────────────────────────
        if r.status_code == 429:
            _invalidate_token()
            if proxy_id:
                _mark_proxy_cooldown(proxy_id, reason="rate_limited")
                log.warning(
                    "Proxy #%s rate_limited (attempt %d/%d) — cooldown",
                    proxy_id, attempt, MAX_CHECK_ATTEMPTS,
                )
            last_failure = "rate_limited"
            if attempt < MAX_CHECK_ATTEMPTS:
                time.sleep(_backoff_delay(attempt) * 2)  # longer pause on rate-limit
            continue

        if r.status_code == 200:
            # Success — promote untested proxy to live
            if proxy_id:
                _mark_proxy_live(proxy_id)
            try:
                data = r.json()
                email_returned = data.get("email", "").lower()
                if email_returned == f"{username}@{domain}".lower():
                    return "available"
                return "taken"
            except Exception:
                # JSON parse failed — treat as error, retry
                last_failure = "api_error"
                if attempt < MAX_CHECK_ATTEMPTS:
                    time.sleep(_backoff_delay(attempt))
                continue

        if r.status_code == 400:
            try:
                err = r.json().get("error", "").lower()
                if "already taken" in err:
                    if proxy_id:
                        _mark_proxy_live(proxy_id)
                    return "taken"
                if "recaptcha" in err:
                    _invalidate_token()
                    last_failure = "captcha_rejected"
                    if attempt < MAX_CHECK_ATTEMPTS:
                        time.sleep(_backoff_delay(attempt))
                    continue
            except Exception:
                pass
            # Unknown 400 — treat as taken (most common cause)
            if proxy_id:
                _mark_proxy_live(proxy_id)
            return "taken"

        # Other status codes — retry with different proxy
        last_failure = f"http_{r.status_code}"
        log.warning(
            "hi2.in returned %s (attempt %d/%d) via %s",
            r.status_code, attempt, MAX_CHECK_ATTEMPTS,
            f"proxy #{proxy_id}" if proxy_id else "direct",
        )
        if attempt < MAX_CHECK_ATTEMPTS:
            time.sleep(_backoff_delay(attempt))

    # All attempts exhausted — return the most specific failure we saw
    log.warning(
        "❌ All %d attempts failed for %s — last_failure=%s",
        MAX_CHECK_ATTEMPTS, email, last_failure,
    )
    # Map internal failure tags to public status keys
    if last_failure in ("captcha_error", "captcha_rejected"):
        return "captcha_error"
    if last_failure == "timeout":
        return "timeout"
    if last_failure in ("connection_error", "conn_err"):
        return "connection_error"
    if last_failure == "rate_limited":
        return "rate_limited"
    if last_failure.startswith("http_") or last_failure == "api_error":
        return "api_error"
    return "error"


# ══════════════════════════════════════════════════════════════════════════════
#  EMAIL PARSING
# ══════════════════════════════════════════════════════════════════════════════

def _parse_emails(raw: str) -> list[str]:
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    seen = set()
    for line in lines:
        parts = re.split(r"[;, \t|]+", line)
        for p in parts:
            p = p.strip()
            if "@" in p and "." in p.split("@")[-1]:
                low = p.lower()
                if low not in seen:
                    seen.add(low)
                    out.append(p)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  ACTIVE TASK REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

_active_user_tasks: dict[int, bool] = {}
_active_lock = Lock()


def _set_user_task(user_id: int, active: bool):
    with _active_lock:
        _active_user_tasks[user_id] = active


def _is_user_busy(user_id: int) -> bool:
    with _active_lock:
        return _active_user_tasks.get(user_id, False)


_cancel_flags: dict[str, bool] = {}
_cancel_lock  = Lock()


def cancel_user_task(task_id: str):
    with _cancel_lock:
        _cancel_flags[task_id] = True


def is_cancelled(task_id: str) -> bool:
    with _cancel_lock:
        return _cancel_flags.get(task_id, False)


# ══════════════════════════════════════════════════════════════════════════════
#  GUARDS
# ══════════════════════════════════════════════════════════════════════════════

def owner_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *a, **kw):
        uid = (update.effective_user or update.callback_query.from_user).id
        if uid != OWNER_ID:
            if update.message:
                await update.message.reply_text("⛔ Owner only.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Owner only.", show_alert=True)
            return
        return await fn(update, ctx, *a, **kw)
    return wrapper


def approved_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *a, **kw):
        user = update.effective_user or update.callback_query.from_user
        row = get_user(user.id)
        if not row:
            if update.callback_query:
                await update.callback_query.answer("Please /start the bot first.", show_alert=True)
            return
        if row["status"] == "PENDING":
            if update.callback_query:
                await update.callback_query.answer(
                    "⏳ Your account is pending approval.", show_alert=True
                )
            return
        if row["status"] in ("REJECTED", "BANNED"):
            if update.callback_query:
                await update.callback_query.answer("⛔ Access denied.", show_alert=True)
            return
        return await fn(update, ctx, *a, **kw)
    return wrapper


def _check_maintenance(uid: int) -> bool:
    """Return True if maintenance is active AND user is not owner."""
    return get_setting("maintenance_mode") == "1" and uid != OWNER_ID


# ══════════════════════════════════════════════════════════════════════════════
#  KEYBOARD BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

# ── Colored buttons (Telegram Bot API `style` field) ────────────────────────
# primary = blue • success = green • danger = red. Rendered on updated
# Telegram clients; older clients silently fall back to the default look.
STYLE_PRIMARY = "primary"
STYLE_SUCCESS = "success"
STYLE_DANGER  = "danger"

# Label hints → red (destructive)
_STYLE_DANGER_HINTS  = ("DELETE", "CLEAR", "BAN", "REMOVE", "STOP", "REVOKE",
                        "REJECT", "BLACKLIST", "MAINTENANCE")
# Label hints → green (money / earn / create / confirm CTA)
_STYLE_SUCCESS_HINTS = ("REFER", "REDEEM", "PREMIUM", "DAILY BONUS",
                        "DAILY TASKS", "CREDITS", "CLAIM", "BUY", "GRANT",
                        "VERIFY", "START CHECKING", "REVIVE", "CONFIRM",
                        "APPROVE", "PENDING", "GIVE", "BROADCAST", "CHECK EMAIL",
                        "UPLOAD")
# Label hints → always blue (navigation / neutral, checked first)
_STYLE_PRIMARY_HINTS = ("CANCEL", "MANAGER", "SETTINGS", "STATS", "CODES",
                        "BACK", "HOME", "PREV", "NEXT")


def _auto_style(text: str) -> str:
    """Pick a button color from its label."""
    t = text.upper()
    if any(h in t for h in _STYLE_PRIMARY_HINTS):
        return STYLE_PRIMARY
    if any(h in t for h in _STYLE_DANGER_HINTS):
        return STYLE_DANGER
    if any(h in t for h in _STYLE_SUCCESS_HINTS):
        return STYLE_SUCCESS
    return STYLE_PRIMARY


def btn(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    """Inline button; auto-colored unless `style` is given.

    The style is passed via `api_kwargs` so it merges into the outgoing
    JSON payload on every PTB version (v20+) — no native field required.
    """
    return InlineKeyboardButton(
        text,
        callback_data=data,
        api_kwargs={"style": style or _auto_style(text)},
    )


def link_btn(text: str, url: str, style: str | None = None) -> InlineKeyboardButton:
    """URL button — JOIN / CTA buttons default to green."""
    return InlineKeyboardButton(
        text,
        url=url,
        api_kwargs={"style": style or STYLE_SUCCESS},
    )


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("CHECK EMAIL", "single_check", STYLE_SUCCESS),  btn("UPLOAD TXT",   "upload_txt", STYLE_SUCCESS)],
        [btn("MY CREDITS",    "my_credits", STYLE_PRIMARY),    btn("DAILY BONUS",  "daily_bonus", STYLE_SUCCESS)],
        [btn("LUCKY SPIN",    "lucky_spin", STYLE_SUCCESS),    btn("DAILY TASKS",   "daily_tasks", STYLE_PRIMARY)],
        [btn("MY ACCOUNT",   "my_account", STYLE_PRIMARY),    btn("PREMIUM PLANS", "premium_plans", STYLE_SUCCESS)],
        [btn("REFER & EARN", "refer_earn", STYLE_SUCCESS),     btn("REDEEM CODE",  "redeem_code", STYLE_PRIMARY)],
        [btn("LEADERBOARD",  "leaderboard", STYLE_PRIMARY),    btn("MY MAIL",       "my_mail", STYLE_PRIMARY)],
        [btn("MY STATS",      "my_stats", STYLE_PRIMARY),       btn("HISTORY",        "my_history", STYLE_PRIMARY)],
        [btn("SUPPORT",       "support", STYLE_PRIMARY),        btn("HELP",           "noop", STYLE_PRIMARY)],
        [btn("REFRESH",        "refresh_home", STYLE_PRIMARY)],
    ])

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("ALL USERS",        "adm_users:0", STYLE_PRIMARY),  btn("PENDING",           "adm_pending", STYLE_SUCCESS)],
        [btn("SEARCH USER",      "adm_search", STYLE_PRIMARY),   btn("BAN / UNBAN",         "adm_ban", STYLE_DANGER)],
        [btn("GIVE CREDITS",     "adm_give", STYLE_SUCCESS),     btn("REMOVE CREDITS",    "adm_remove", STYLE_DANGER)],
        [btn("PREMIUM MANAGER",  "adm_premium", STYLE_SUCCESS),  btn("BONUS SETTINGS",    "adm_bonus_settings", STYLE_PRIMARY)],
        [btn("REFERRAL SETTINGS","adm_referral_settings", STYLE_PRIMARY), btn("REDEEM CODES", "adm_redeem_manager", STYLE_PRIMARY)],
        [btn("TASK MANAGER",     "adm_task_manager", STYLE_PRIMARY), btn("MAIL MANAGER",  "adm_mail_manager", STYLE_PRIMARY)],
        [btn("REWARDS HUB",      "adm_rewards", STYLE_SUCCESS),  btn("FJ CHANNELS",       "adm_fj", STYLE_PRIMARY)],
        [btn("START MSG",        "adm_startmsg", STYLE_PRIMARY), btn("TASK/MAIL STATS",  "adm_task_mail_stats", STYLE_PRIMARY)],
        [btn("BOT STATS",         "adm_stats", STYLE_PRIMARY),    btn("TOGGLE CHECKING",  "adm_toggle_check", STYLE_PRIMARY)],
        [btn("MAINTENANCE",       "adm_toggle_maint", STYLE_DANGER), btn("PROXY MANAGER", "adm_proxy_manager", STYLE_PRIMARY)],
        [btn("BACKUP DB",        "adm_backup", STYLE_PRIMARY),    btn("BACKUP HISTORY",   "adm_backup_list", STYLE_PRIMARY)],
        [btn("AUDIT LOGS",        "adm_audit", STYLE_PRIMARY),     btn("ERROR LOGS",       "adm_error_logs", STYLE_PRIMARY)],
        [btn("BLACKLIST",         "adm_blacklist", STYLE_DANGER),  btn("BROADCAST",        "adm_broadcast", STYLE_SUCCESS)],
        [btn("ADMIN HOME",        "adm_back", STYLE_PRIMARY)],
    ])


def back_to_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("🏠 HOME", "refresh_home")]])


def back_to_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("🔙 ADMIN PANEL", "adm_panel")]])


# Sub-panel aliases — now all redirect to flat admin panel
def adm_users_panel_kb() -> InlineKeyboardMarkup:
    return admin_menu_kb()

def adm_tasks_panel_kb() -> InlineKeyboardMarkup:
    return admin_menu_kb()

def adm_system_panel_kb() -> InlineKeyboardMarkup:
    return admin_menu_kb()


def confirm_kb(yes_data: str, no_data: str = "adm_panel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("CONFIRM", yes_data, STYLE_SUCCESS), btn("CANCEL", no_data, STYLE_DANGER)]
    ])


# ══════════════════════════════════════════════════════════════════════════════
#  REWARDS SYSTEM v4 — Force-Join Gate • Referral 2-Day Lock • Streak
#                     Lucky Spin • VIP Ranks • Withdraw • Jobs
# ══════════════════════════════════════════════════════════════════════════════

async def _try_notify(bot, chat_id: int, text: str, kb=None):
    try:
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        pass


# ── Force-Join GATE (mandatory channels before bot use) ─────────────────────

def fj_gate_enabled() -> bool:
    return get_setting("fj_gate_enabled", "1") == "1"


def fj_gate_channels(active_only: bool = True):
    q = "SELECT * FROM fj_gate_channels" + (" WHERE is_active=1" if active_only else "") \
        + " ORDER BY id"
    with get_db() as db:
        return db.execute(q).fetchall()


def fj_gate_add(channel_id: str, title: str = "") -> bool:
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO fj_gate_channels(channel_id, title) VALUES(?,?) "
                "ON CONFLICT(channel_id) DO NOTHING",
                (channel_id, title),
            )
        return True
    except Exception as e:
        log.warning("fj_gate_add failed: %s", e)
        return False


def fj_gate_remove(ch_id: str):
    with get_db() as db:
        db.execute("DELETE FROM fj_gate_channels WHERE channel_id=?", (ch_id,))


def fj_gate_toggle(ch_id: str) -> str:
    with get_db() as db:
        row = db.execute(
            "SELECT is_active FROM fj_gate_channels WHERE channel_id=?", (ch_id,)
        ).fetchone()
        if not row:
            return "❌ Not found."
        new = 0 if row["is_active"] else 1
        db.execute(
            "UPDATE fj_gate_channels SET is_active=? WHERE channel_id=?", (new, ch_id))
    return f"{'✅ ON' if new else '❌ OFF'} — {ch_id}"


_FJ_NUM_CACHE: dict = {}


async def _fj_resolve_num(bot, stored_id: str):
    if stored_id in _FJ_NUM_CACHE:
        return _FJ_NUM_CACHE[stored_id]
    try:
        chat = await bot.get_chat(stored_id)
        _FJ_NUM_CACHE[stored_id] = chat.id
        return chat.id
    except Exception:
        return None


async def check_fj_gate(bot, user_id: int) -> tuple[bool, list]:
    """Real membership check on all ACTIVE gate channels."""
    missing = []
    for ch in fj_gate_channels(active_only=True):
        ok, _ = await check_channel_membership(bot, user_id, ch["channel_id"])
        if not ok:
            missing.append(ch)
    return len(missing) == 0, missing


def set_channel_ok(user_id: int, ok: bool):
    with get_db() as db:
        db.execute(
            "UPDATE users SET channel_ok=? WHERE telegram_id=?",
            (1 if ok else 0, user_id),
        )


async def _gate_status(bot, user_id: int, force: bool = False) -> tuple[bool, list]:
    """Fast cached gate check (force=True → real API check)."""
    if not fj_gate_enabled():
        return True, []
    row = get_user(user_id)
    if row and row["channel_ok"] and not force:
        return True, []
    ok, missing = await check_fj_gate(bot, user_id)
    if ok:
        set_channel_ok(user_id, True)
    return ok, missing


def fj_gate_text(name: str) -> str:
    return (
        f"{HEADER}\n{DIV}\n"
        f"🔒 *CHANNEL JOIN REQUIRED*\n{SUB}\n"
        f"👋 {name}, bot use karne ke liye pehle ye channel(s) "
        f"*JOIN* karo:\n\n"
        f"_Join karne ke baad ✅ VERIFY JOIN dabao._"
    )


def fj_gate_kb(missing: list) -> InlineKeyboardMarkup:
    kb = []
    for ch in missing:
        cid = ch["channel_id"]
        label = ch["title"] or (f"@{cid.lstrip('@')}" if cid.startswith("@") else cid)
        url = f"https://t.me/{cid.lstrip('@')}" if cid.startswith("@") else \
              f"https://t.me/{cid}"
        kb.append([link_btn(f"➡️ JOIN {label}", url)])
    kb.append([btn("✅ VERIFY JOIN", "fj_verify")])
    kb.append([btn("🔄 REFRESH", "fj_verify")])
    return InlineKeyboardMarkup(kb)


# ── Referral lifecycle: PENDING → LOCK (2 din) / PENALIZED (early leave) ────

def _within_stay_window(ref_joined_at: str) -> bool:
    stay = int(get_setting("stay_days", "2"))
    try:
        then = datetime.strptime(ref_joined_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return True
    return datetime.utcnow() < then + timedelta(days=stay)


def referral_pending_start(referee_id: int) -> "int | None":
    """Gate pass hote hi referral clock start (ref_joined_at set)."""
    with get_db() as db:
        r = db.execute(
            "SELECT id, referrer_id, state, ref_joined_at FROM referrals "
            "WHERE referee_id=?", (referee_id,)
        ).fetchone()
        if not r or (r["state"] or "LOCKED") != "PENDING" or r["ref_joined_at"]:
            return None
        db.execute(
            "UPDATE referrals SET ref_joined_at=? WHERE id=?", (_now_utc(), r["id"]))
        return r["referrer_id"]


def referral_lock(referee_id: int, grant: bool = True) -> "int | None":
    """stay_days pure hone pe referral LOCK + referrer ko reward."""
    with get_db() as db:
        r = db.execute(
            "SELECT id, referrer_id, state, reward_given FROM referrals "
            "WHERE referee_id=?", (referee_id,)
        ).fetchone()
        if not r or (r["state"] or "LOCKED") != "PENDING":
            return None
        db.execute("UPDATE referrals SET state='LOCKED' WHERE id=?", (r["id"],))
        referrer = r["referrer_id"]
        if grant and not r["reward_given"]:
            reward = int(get_setting("referral_reward", "25"))
            db.execute(
                "UPDATE referrals SET reward_given=? WHERE id=?", (reward, r["id"]))
        else:
            reward = 0
    if grant and reward:
        add_credits(referrer, reward, "referral_reward")
        log_audit(0, "referral_lock", referee_id,
                  f"referrer={referrer} reward={reward}")
    return referrer


def referral_penalize(referee_id: int, channel_hint: str = "") -> "tuple | None":
    """Early leave (window ke andar) → referrer se penalty (minus allowed).
    Window guzar chuki ho to referral lock+reward ho jata hai (no penalty).
    NOTE: sab ek hi transaction me — nested connections SQLite deadlock karte."""
    result = None
    with get_db() as db:
        r = db.execute(
            "SELECT id, referrer_id, state, ref_joined_at FROM referrals "
            "WHERE referee_id=?", (referee_id,)
        ).fetchone()
        if (r and (r["state"] or "LOCKED") == "PENDING"
                and r["ref_joined_at"]
                and _within_stay_window(r["ref_joined_at"])):
            penalty = int(get_setting("penalty_amount", "25"))
            db.execute(
                "UPDATE referrals SET state='PENALIZED', penalty=? WHERE id=?",
                (penalty, r["id"]),
            )
            db.execute(
                "UPDATE users SET credits=credits-? WHERE telegram_id=?",
                (penalty, r["referrer_id"]),
            )
            db.execute(
                "INSERT INTO credit_transactions(user_id,amount,reason,actor_id) "
                "VALUES(?,?,?,0)",
                (r["referrer_id"], -penalty, "referral_penalty"),
            )
            result = (r["referrer_id"], penalty)
    if result:
        log_audit(0, "referral_penalty", referee_id,
                  f"referrer={result[0]} penalty={result[1]} channel={channel_hint}")
        return result
    # Window guzar chuki thi → earned, safe lock (no penalty)
    referral_lock(referee_id, grant=True)
    return None


async def referral_lock_sweep(bot) -> int:
    """Job: expire hui PENDING referrals check karo.
    Member abhi bhi hai → LOCK + reward. Nahi hai → penalize (agar window me)."""
    stay = int(get_setting("stay_days", "2"))
    cutoff = (datetime.utcnow() - timedelta(days=stay)).strftime("%Y-%m-%d %H:%M:%S")
    acted = 0
    with get_db() as db:
        rows = db.execute(
            "SELECT referee_id FROM referrals WHERE state='PENDING' "
            "AND ref_joined_at IS NOT NULL AND ref_joined_at < ?",
            (cutoff,),
        ).fetchall()
    for r in rows:
        uid = r["referee_id"]
        chans = fj_gate_channels(active_only=True)
        if chans:
            still = all(
                (await check_channel_membership(bot, uid, ch["channel_id"]))[0]
                for ch in chans
            )
        else:
            still = True  # gate channels hi nahi → sab safe
        if still:
            referrer = referral_lock(uid, grant=True)
            if referrer:
                acted += 1
                reward = int(get_setting("referral_reward", "25"))
                stay_d = get_setting("stay_days", "2")
                await _try_notify(
                    bot, referrer,
                    f"{HEADER}\n{DIV}\n🔒 *REFERRAL LOCKED!* ✅\n{SUB}\n"
                    f"👤 User `{uid}` ne *{stay_d} din* channel me bita diye.\n"
                    f"💰 Your `+{reward}` credits are now *SAFE*! 🎉")
                await _try_notify(
                    bot, uid,
                    f"{HEADER}\n{DIV}\n🎉 *REFERRAL CONFIRMED!*\n{SUB}\n"
                    f"👤 Jo aapke through aya tha, usne {stay_d} din pura kar liya.\n"
                    f"💰 Uske referrer ko reward mil gaya. Thanks! 🙏")
        else:
            res = referral_penalize(uid, "sweep")
            if res:
                acted += 1
                referrer, penalty = res
                stay_d = get_setting("stay_days", "2")
                await _try_notify(
                    bot, referrer,
                    f"{HEADER}\n{DIV}\n⚠️ *REFERRAL PENALTY*\n{SUB}\n"
                    f"👤 User `{uid}` ne {stay_d} din se *pehle* channel chhod diya.\n"
                    f"💸 Penalty: `-{penalty}` credits (your account)")
                await _try_notify(
                    bot, uid,
                    f"⚠️ Aap channel se nikal gaye the — jisne refer kiya uska "
                    f"credit kata gaya. Wapas join karke bot use kar sakte ho.")
    return acted


def top_referrers(limit: int = 10):
    with get_db() as db:
        return db.execute(
            "SELECT r.referrer_id AS uid, "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN COALESCE(r.state,'LOCKED')='LOCKED' THEN 1 ELSE 0 END) AS locked, "
            "  COALESCE(SUM(r.reward_given),0) AS earned, "
            "  u.full_name AS full_name, u.username AS username "
            "FROM referrals r LEFT JOIN users u ON u.telegram_id = r.referrer_id "
            "GROUP BY r.referrer_id ORDER BY locked DESC, earned DESC LIMIT ?",
            (limit,),
        ).fetchall()


async def weekly_top_broadcast(bot):
    """Sunday UTC 18:00 — top 10 referrers sab APPROVED users ko."""
    top = top_referrers(10)
    if not top:
        return
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = []
    for i, e in enumerate(top):
        name = e["full_name"] or f"User {e['uid']}"
        uname = f" (@{e['username']})" if e["username"] else ""
        lines.append(f"{medals[i]} {name[:20]}{uname} — ✅ {e['locked']} locked, 💰 {e['earned']}")
    text = (
        f"{HEADER}\n{DIV}\n"
        f"🏆 *WEEKLY TOP REFERRERS*\n{SUB}\n" + "\n".join(lines) +
        f"\n\n👥 _Refer & Earn — har approved referral +"
        f"{get_setting('referral_reward', '25')} credits!_"
    )
    with get_db() as db:
        ids = [r["telegram_id"] for r in db.execute(
            "SELECT telegram_id FROM users WHERE status='APPROVED'").fetchall()]
    for uid in ids:
        await _try_notify(bot, uid, text)
    log.info("Weekly top broadcast sent to %s users", len(ids))


# ── Lucky Spin ───────────────────────────────────────────────────────────────

def do_lucky_spin(user_id: int) -> tuple[bool, int, str]:
    if get_setting("spin_enabled", "1") != "1":
        return False, 0, "🎰 Spin is currently disabled."
    today = today_utc()
    mn = int(get_setting("spin_min", "5"))
    mx = int(get_setting("spin_max", "50"))
    with _db_lock:
        with get_db() as db:
            row = db.execute(
                "SELECT last_spin FROM lucky_spins WHERE user_id=?", (user_id,)
            ).fetchone()
            if row and row["last_spin"] == today:
                return False, 0, "⏰ Aaj ka spin ho chuka — kal phir try karo!"
            won = random.randint(mn, mx)
            db.execute(
                """INSERT INTO lucky_spins(user_id, last_spin, spins, total_won)
                   VALUES(?,?,1,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     last_spin=excluded.last_spin,
                     spins=lucky_spins.spins+1,
                     total_won=lucky_spins.total_won+excluded.total_won""",
                (user_id, today, won),
            )
    add_credits(user_id, won, "lucky_spin")
    return True, won, f"🎰 You won *+{won}* credits!"


# ── VIP Ranks (credits se compute hota hai) ─────────────────────────────────

def vip_rank(credits: int) -> tuple:
    silver = int(get_setting("vip_silver", "100"))
    gold = int(get_setting("vip_gold", "300"))
    diamond = int(get_setting("vip_diamond", "600"))
    if credits >= diamond:
        return "💎 DIAMOND", None
    if credits >= gold:
        return "🥇 GOLD", diamond
    if credits >= silver:
        return "🥈 SILVER", gold
    return "🥉 BRONZE", silver


# ── Admin sub-screens (rewards hub / FJ manager) ───────────────────────────

_INT_SETTING_KEYS = {
    "daily_bonus_amount", "referral_reward", "stay_days", "penalty_amount",
    "streak_days", "streak_bonus", "spin_min", "spin_max",
    "vip_silver", "vip_gold", "vip_diamond",
}


def _rewards_hub_payload() -> tuple:
    s = {k: get_setting(k, "") for k in (
        "daily_bonus_amount", "referral_reward", "stay_days", "penalty_amount",
        "streak_days", "streak_bonus", "spin_min", "spin_max",
        "vip_silver", "vip_gold", "vip_diamond")}
    t = {k: "ON ✅" if get_setting(k, "0") == "1" else "OFF ❌" for k in (
        "daily_bonus_enabled", "spin_enabled",
        "weekly_top_enabled", "auto_backup_enabled", "fj_gate_enabled")}
    text = (
        f"🎁 *REWARDS HUB*\n{DIV}\n"
        f"💰 Daily: `{s['daily_bonus_amount']}`  •  👥 Referral: `{s['referral_reward']}`\n"
        f"⏳ Stay: `{s['stay_days']} din`  •  ⚠️ Penalty: `{s['penalty_amount']}`\n"
        f"🔥 Streak: `{s['streak_days']} din / +{s['streak_bonus']}`\n"
        f"🎰 Spin: `{t['spin_enabled']}` (`{s['spin_min']}-{s['spin_max']}`)\n"
        f"💎 VIP: `S{s['vip_silver']} G{s['vip_gold']} D{s['vip_diamond']}`\n"
        f"📢 Weekly top: `{t['weekly_top_enabled']}`  •  💾 AutoBackup: `{t['auto_backup_enabled']}`\n"
        f"🔒 FJ Gate: `{t['fj_gate_enabled']}`  •  🎁 Daily bonus: `{t['daily_bonus_enabled']}`\n\n"
        f"_Kisi value ko change karna ho to button dabao._"
    )
    kb = InlineKeyboardMarkup([
        [btn("💰 DAILY AMT", "adm_set:daily_bonus_amount"),
         btn("👥 REF BONUS", "adm_set:referral_reward")],
        [btn("⏳ STAY DAYS", "adm_set:stay_days"),
         btn("⚠️ PENALTY", "adm_set:penalty_amount")],
        [btn("🔥 STREAK DIN", "adm_set:streak_days"),
         btn("🎁 STREAK BONUS", "adm_set:streak_bonus")],
        [btn("🎰 SPIN MIN", "adm_set:spin_min"),
         btn("🎯 SPIN MAX", "adm_set:spin_max")],
        [btn("🥈 VIP SILVER", "adm_set:vip_silver"),
         btn("🥇 VIP GOLD", "adm_set:vip_gold"),
         btn("💠 VIP DIAM", "adm_set:vip_diamond")],
        [btn(f"🎰 SPIN {t['spin_enabled'][:2]}", "adm_rew_toggle:spin_enabled")],
        [btn(f"📢 WEEKLY {t['weekly_top_enabled'][:2]}", "adm_rew_toggle:weekly_top_enabled"),
         btn(f"💾 BACKUP {t['auto_backup_enabled'][:2]}", "adm_rew_toggle:auto_backup_enabled")],
        [btn(f"🔒 GATE {t['fj_gate_enabled'][:2]}", "adm_rew_toggle:fj_gate_enabled"),
         btn(f"🎁 DAILY {t['daily_bonus_enabled'][:2]}", "adm_rew_toggle:daily_bonus_enabled")],
        [btn("🔙 BACK", "adm_panel")],
    ])
    return text, kb


async def _show_rewards_hub(q):
    text, kb = _rewards_hub_payload()
    try:
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        pass


def _fj_manager_payload() -> tuple:
    chans = fj_gate_channels(active_only=False)
    gate = "ON ✅" if fj_gate_enabled() else "OFF ❌"
    kb, lines = [], []
    for ch in chans:
        flag = "🟢" if ch["is_active"] else "⚪"
        lines.append(f"{flag} `{ch['channel_id']}` — {ch['title'][:20]}")
        kb.append([
            btn("ON/OFF", f"adm_fj_toggle:{ch['channel_id']}"),
            btn("🗑 DELETE", f"adm_fj_del:{ch['channel_id']}"),
        ])
    kb.insert(0, [btn("➕ ADD CHANNEL", "adm_fj_add"),
                  btn(f"🔒 GATE {gate[:2]}", "adm_rew_toggle:fj_gate_enabled")])
    kb.append([btn("🔙 BACK", "adm_panel")])
    text = (
        f"🔒 *FORCE JOIN CHANNELS*\n{DIV}\n"
        f"Gate: *{gate}*  •  Channels: `{len(chans)}`\n\n"
        + ("\n".join(lines) if lines else "_No channels added — ADD karo!_") +
        f"\n\n⚠️ _Bot ko channel me ADMIN banana zaroori hai!_"
    )
    return text, InlineKeyboardMarkup(kb)


async def _show_fj_manager(q):
    text, kb = _fj_manager_payload()
    try:
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        pass





# ── ChatMember tracking (join/leave) — bot must be ADMIN in gate channels ───

async def on_chat_member_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if cm is None:
        return
    if not fj_gate_enabled():
        return
    u = cm.new_chat_member.user
    if u.is_bot:
        return
    chat_num = update.effective_chat.id
    tracked = False
    for ch in fj_gate_channels(active_only=True):
        num = await _fj_resolve_num(ctx.bot, ch["channel_id"])
        if num == chat_num:
            tracked = True
            break
    if not tracked:
        return

    was = cm.old_chat_member.status in MEMBER_STATUSES
    now = cm.new_chat_member.status in MEMBER_STATUSES
    uid = u.id

    if now and not was:  # JOIN
        row = get_user(uid)
        if row and not row["channel_ok"]:
            set_channel_ok(uid, True)
            ref_id = referral_pending_start(uid)
            if ref_id:
                stay = get_setting("stay_days", "2")
                reward = get_setting("referral_reward", "25")
                await _try_notify(
                    ctx.bot, ref_id,
                    f"{HEADER}\n{DIV}\n🎉 *REFERRAL JOINED CHANNEL!*\n{SUB}\n"
                    f"👤 *{u.first_name}* ne channel join kiya — clock started!\n"
                    f"⏳ `{stay} din` baad referral LOCK hoga → `+{reward}` safe ✅")
            await _try_notify(
                ctx.bot, uid,
                f"{HEADER}\n{DIV}\n✅ *JOIN VERIFIED!*\n{SUB}\n"
                f"🚀 /start dabao aur bot ka maza lo!")
    elif was and not now:  # LEAVE
        row = get_user(uid)
        if row and row["channel_ok"]:
            set_channel_ok(uid, False)
        res = referral_penalize(uid, str(chat_num))
        if res:
            referrer, penalty = res
            stay = get_setting("stay_days", "2")
            await _try_notify(
                ctx.bot, referrer,
                f"{HEADER}\n{DIV}\n⚠️ *REFERRAL PENALTY*\n{SUB}\n"
                f"👤 *{u.first_name}* ne {stay} din se pehle channel leave kiya!\n"
                f"💸 Penalty: `-{penalty}` credits")
            await _try_notify(
                ctx.bot, uid,
                f"⚠️ Tune channel leave kiya — referral penalty lag gayi.\n"
                f"🚀 Wapas join karke bot chala sakte ho.")


# ── Background jobs (no job-queue dependency — asyncio loop) ────────────────

async def _jobs_loop(app):
    last_sweep = 0.0
    while True:
        try:
            now = time.time()
            if now - last_sweep >= 3 * 3600:
                last_sweep = now
                n = await referral_lock_sweep(app.bot)
                if n:
                    log.info("Referral sweep acted on %s referrals", n)
            # weekly top referrers — Sunday 18:00 UTC
            if get_setting("weekly_top_enabled", "1") == "1":
                utcnow = datetime.now(timezone.utc)
                if utcnow.weekday() == 6 and utcnow.hour >= 18:
                    if get_setting("weekly_top_last", "") != today_utc():
                        await weekly_top_broadcast(app.bot)
                        set_setting("weekly_top_last", today_utc())
            # auto backup — once per day
            if get_setting("auto_backup_enabled", "0") == "1":
                if get_setting("last_backup_date", "") != today_utc():
                    msg = await do_database_backup(app.bot)
                    if "✅" in msg:
                        set_setting("last_backup_date", today_utc())
        except Exception as e:
            log.warning("jobs loop error: %s", e)
        await asyncio.sleep(600)


# ══════════════════════════════════════════════════════════════════════════════
#  CHANNEL MEMBERSHIP VERIFICATION
# ══════════════════════════════════════════════════════════════════════════════

MEMBER_STATUSES = {"member", "administrator", "creator", "restricted"}


async def check_channel_membership(bot, user_id: int, channel_id_str: str) -> tuple[bool, str]:
    """
    Returns (is_member, error_or_empty).
    channel_id_str can be '@username' or '-100XXXXXXXXX'.
    """
    try:
        chat_id = (
            int(channel_id_str)
            if channel_id_str.lstrip("-").isdigit()
            else channel_id_str
        )
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in MEMBER_STATUSES, ""
    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE BACKUP
# ══════════════════════════════════════════════════════════════════════════════

async def do_database_backup(bot) -> str:
    global _DB_PATH
    if USING_PG:
        return await _pg_database_backup(bot)
    if not _DB_PATH or _DB_PATH == ":memory:":
        return "❌ In-memory database cannot be backed up to a file."

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"hi2_backup_{ts}.db"
    backup_path = os.path.join(BACKUP_DIR, filename)

    try:
        # Use SQLite's built-in backup API for consistency
        src  = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30)
        dest = sqlite3.connect(backup_path, check_same_thread=False)
        with src:
            src.backup(dest)
        dest.close()
        src.close()

        size = os.path.getsize(backup_path)

        with _db_lock:
            with get_db() as db:
                db.execute(
                    "INSERT INTO db_backups(filename, size_bytes, result) VALUES(?,?,?)",
                    (filename, size, "ok"),
                )

        with open(backup_path, "rb") as f:
            await bot.send_document(
                OWNER_ID,
                document=f,
                filename=filename,
                caption=(
                    f"💾 *Database Backup*\n{DIV}\n"
                    f"📄 `{filename}`\n"
                    f"📦 Size: `{size:,}` bytes\n"
                    f"🕐 `{ts} UTC`"
                ),
                parse_mode="Markdown",
            )
        return f"✅ Backup complete — `{filename}` ({size:,} bytes)"

    except Exception as e:
        log.error("Backup failed: %s", e)
        try:
            with get_db() as db:
                db.execute(
                    "INSERT INTO db_backups(filename, size_bytes, result) VALUES(?,?,?)",
                    (filename, 0, f"error:{e}"),
                )
        except Exception:
            pass
        return f"❌ Backup failed: {e}"


_BACKUP_TABLES = (
    "users", "credit_transactions", "tasks", "task_results", "audit_logs",
    "settings", "daily_bonus", "premium", "referrals", "redeem_codes",
    "redemptions", "force_join_tasks", "force_join_channels",
    "task_completions", "hi2_mail", "db_backups", "proxies",
    "fj_gate_channels", "lucky_spins", "withdraw_requests", "panel_admins",
)


async def _pg_database_backup(bot) -> str:
    """Postgres backup: saari tables JSON file me dump + owner ko bhejo."""
    import json as _json
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"hi2_backup_{ts}.json"
    backup_path = os.path.join(BACKUP_DIR, filename)
    try:
        dump = {}
        with get_db() as db:
            for t in _BACKUP_TABLES:
                try:
                    rows = db.execute(f"SELECT * FROM {t}").fetchall()
                    dump[t] = [dict(r) for r in rows]
                except Exception:
                    dump[t] = []
        with open(backup_path, "w", encoding="utf-8") as f:
            _json.dump(dump, f, default=str, ensure_ascii=False)
        size = os.path.getsize(backup_path)
        with get_db() as db:
            db.execute(
                "INSERT INTO db_backups(filename, size_bytes, result) VALUES(?,?,?)",
                (filename, size, "ok"),
            )
        with open(backup_path, "rb") as f:
            await bot.send_document(
                OWNER_ID, document=f, filename=filename,
                caption=(f"💾 *Database Backup (Postgres JSON)*\n{DIV}\n"
                         f"📄 `{filename}`\n📦 Size: `{size:,}` bytes\n"
                         f"🕐 `{ts} UTC`"),
                parse_mode="Markdown",
            )
        return f"✅ Backup complete — `{filename}` ({size:,} bytes)"
    except Exception as e:
        log.error("PG backup failed: %s", e)
        try:
            with get_db() as db:
                db.execute(
                    "INSERT INTO db_backups(filename, size_bytes, result) VALUES(?,?,?)",
                    (filename, 0, f"error:{e}"),
                )
        except Exception:
            pass
        return f"❌ Backup failed: {e}"


# ══════════════════════════════════════════════════════════════════════════════
#  /start COMMAND
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args  # list of words after /start

    is_new = register_user(user)

    # Handle referral deep-link: /start ref_XXXXXXXX
    if args and args[0].startswith("ref_"):
        ref_code = args[0][4:]
        if is_new:
            attached = apply_referral(user.id, ref_code)
            if attached:
                referrer_row = None
                with get_db() as db:
                    refrow = db.execute(
                        "SELECT referrer_id FROM referrals WHERE referee_id=?", (user.id,)
                    ).fetchone()
                    if refrow:
                        referrer_row = refrow["referrer_id"]
                if referrer_row:
                    try:
                        stay = get_setting("stay_days", "2")
                        reward = get_setting("referral_reward", "25")
                        await ctx.bot.send_message(
                            referrer_row,
                            f"{HEADER}\n{DIV}\n"
                            f"👥 *NEW REFERRAL!* 👀\n{SUB}\n"
                            f"👤 `{user.full_name}` joined using your link.\n"
                            f"⏳ _Channel join karke {stay} din rahega to_ "
                            f"`+{reward}` _credits milenge!_ 🔒",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass

    row = get_user(user.id)

    if _check_maintenance(user.id):
        await update.message.reply_text(
            f"{HEADER}\n{DIV}\n"
            f"🔧 *UNDER MAINTENANCE*\n\n"
            f"_The bot is being upgraded. Back soon!_ 🛠",
            parse_mode="Markdown",
        )
        return

    # Owner can never be stuck in PENDING
    if row and user.id == OWNER_ID and row["status"] != "APPROVED":
        with get_db() as db:
            db.execute(
                "UPDATE users SET status='APPROVED' WHERE telegram_id=?",
                (user.id,),
            )
        row = get_user(user.id)

    if (is_new or (row and row["status"] == "PENDING")) and user.id != OWNER_ID:
        await update.message.reply_text(
            f"{HEADER}\n{DIV}\n"
            f"👋 Welcome, *{user.full_name}*!\n{SUB}\n"
            f"⏳ Your account is *pending approval*.\n"
            f"The owner will review your request shortly.\n\n"
            f"🔔 You'll get a message once approved!",
            parse_mode="Markdown",
        )
        if is_new:
            await ctx.bot.send_message(
                OWNER_ID,
                f"🔔 *NEW USER REGISTRATION*\n{DIV}\n"
                f"👤 Name: `{user.full_name}`\n"
                f"🆔 ID: `{user.id}`\n"
                f"🔗 @{user.username or 'N/A'}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [
                        btn(f"✅ APPROVE {user.id}", f"approve:{user.id}"),
                        btn(f"❌ REJECT {user.id}",  f"reject:{user.id}"),
                    ]
                ]),
            )
        return

    if row and row["status"] in ("REJECTED", "BANNED"):
        await update.message.reply_text("⛔ Access denied.")
        return

    # ── Force-Join GATE (mandatory channel join) — owner bypass ──────────────
    if user.id != OWNER_ID:
        gok, gmissing = await _gate_status(ctx.bot, user.id, force=True)
        if not gok:
            await update.message.reply_text(
                fj_gate_text(user.full_name),
                parse_mode="Markdown",
                reply_markup=fj_gate_kb(gmissing),
            )
            return
        # Gate pass → referral clock start (agar referral se aya hai)
        ref_id = referral_pending_start(user.id)
        if ref_id:
            stay = get_setting("stay_days", "2")
            reward = get_setting("referral_reward", "25")
            await _try_notify(
                ctx.bot, ref_id,
                f"{HEADER}\n{DIV}\n🎉 *REFERRAL JOINED CHANNEL!*\n{SUB}\n"
                f"👤 *{user.full_name}* ne channel join kiya — clock started!\n"
                f"⏳ `{stay} din` baad referral LOCK hoga → `+{reward}` safe ✅")

    prem = "💎 PREMIUM" if is_premium(user.id) else "🆓 FREE"
    vip_name, _ = vip_rank(row["credits"] if row else 0)
    custom = get_setting("custom_start_text", "").strip()
    welcome_body = (
        f"{HEADER}\n{DIV}\n"
        f"👋 Welcome back, *{user.full_name}*!\n"
        f"🏷 Plan: {prem}  •  {vip_name}  •  💰 Credits: `{row['credits']}`\n"
        f"{SUB}\n"
    )
    if custom:
        welcome_body += f"{custom}\n{SUB}\n👇 _Choose an option below_"
    else:
        welcome_body += "👇 _Choose an option below_"
    await update.message.reply_text(
        welcome_body,
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /admin COMMAND
# ══════════════════════════════════════════════════════════════════════════════

def _admin_dashboard_text() -> str:
    """Live stats dashboard shown on /admin and the owner-panel button."""
    try:
        with get_db() as db:
            total_users    = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            approved_users = db.execute("SELECT COUNT(*) AS c FROM users WHERE status='APPROVED'").fetchone()["c"]
            pending_users  = db.execute("SELECT COUNT(*) AS c FROM users WHERE status='PENDING'").fetchone()["c"]
            banned_users   = db.execute("SELECT COUNT(*) AS c FROM users WHERE status='BANNED'").fetchone()["c"]
            credits_sum    = db.execute("SELECT COALESCE(SUM(credits),0) AS s FROM users").fetchone()["s"]
            total_prem     = db.execute("SELECT COUNT(*) AS c FROM premium").fetchone()["c"]
            total_tasks    = db.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
            total_avail    = db.execute("SELECT COALESCE(SUM(available),0) AS s FROM tasks").fetchone()["s"]
            total_checks   = db.execute("SELECT COALESCE(SUM(total_checks),0) AS s FROM users").fetchone()["s"]
        ps         = proxy_stats()
        checking_on = get_setting("checking_enabled") == "1"
        maint_on    = get_setting("maintenance_mode") == "1"
        up     = max(0, int(time.time() - BOT_BOOT_TIME))
        uptime = f"{up // 3600}h {up % 3600 // 60:02d}m" if up >= 3600 else f"{max(up // 60, 1)}m"
        return (
            f"{HEADER}\n{DIV}\n"
            f"🛡 *OWNER PANEL*  ⏱ _uptime {uptime}_\n{SUB}\n"
            f"👥 Users: `{total_users}` — ✅ {approved_users} | ⏳ {pending_users} | 🚫 {banned_users}\n"
            f"`{mini_bar(approved_users, max(total_users, 1))}`\n"
            f"💰 Credits circulating: `{credits_sum}`\n"
            f"💎 Premium members: `{total_prem}`\n"
            f"📋 Tasks: `{total_tasks}`  •  ✅ Available: `{total_avail}`\n"
            f"🔍 Total checks: `{total_checks}`\n{SUB}\n"
            f"🌐 Proxies: live {ps['live']}  dead {ps['dead']}  new {ps['untested']}\n"
            f"`{mini_bar(ps['live'], max(ps['total'], 1))}`\n"
            f"⚙️ Checking: {'ON' if checking_on else 'OFF'}  •  🔧 Maint: {'ON' if maint_on else 'OFF'}\n"
            f"{DIV}\n"
            f"👇 _Select a section_"
        )
    except Exception as e:                     # DB hiccup → plain panel
        logging.getLogger(__name__).warning("owner dashboard fallback: %s", e)
        return (
            f"{HEADER}\n{DIV}\n"
            f"🛡 *OWNER PANEL*\n{SUB}\n"
            f"👇 _Select a section_"
        )


@owner_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(
            _admin_dashboard_text(),
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(),
        )
    except Exception as e:
        log.error("cmd_admin failed: %s", e)
        await update.message.reply_text(
            f"{HEADER}\n{DIV}\n🛡 *OWNER PANEL*\n{SUB}\n👇 _Select a section_",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  /cancel COMMAND
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "✖️ *Cancelled* — you're back at home.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /live COMMAND — owner live dashboard (v94)
# ══════════════════════════════════════════════════════════════════════════════

@owner_only
async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Quick live snapshot for owner."""
    uptime_s = int(time.time() - BOT_BOOT_TIME)
    h, rem = divmod(uptime_s, 3600)
    m, s = divmod(rem, 60)
    with get_db() as db:
        total_users = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        approved = db.execute("SELECT COUNT(*) AS c FROM users WHERE status='APPROVED'").fetchone()["c"]
        pending = db.execute("SELECT COUNT(*) AS c FROM users WHERE status='PENDING'").fetchone()["c"]
        running = db.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='RUNNING'").fetchone()["c"]
        today_checks = db.execute(
            "SELECT COALESCE(SUM(processed),0) AS s FROM tasks "
            "WHERE created_at >= date('now')"
        ).fetchone()["s"]
    pstats = proxy_stats()
    busy = sum(1 for v in _active_user_tasks.values() if v)
    text = (
        f"{HEADER}\n{DIV}\n"
        f"📡 *LIVE DASHBOARD*\n{SUB}\n"
        f"⏱ Uptime: `{h}h {m}m {s}s`\n"
        f"👥 Users: `{total_users}` (✅{approved} ⏳{pending})\n"
        f"⚙️ Running tasks: `{running}`  •  Busy users: `{busy}`\n"
        f"🔍 Checks today: `{today_checks}`\n"
        f"🌐 Proxies: live {pstats['live']}  dead {pstats['dead']}  new {pstats['untested']} / {pstats['total']}\n"
        f"🔧 Checking: `{'ON' if get_setting('checking_enabled')=='1' else 'OFF'}`\n"
        f"🛠 Maintenance: `{'ON' if get_setting('maintenance_mode')=='1' else 'OFF'}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=admin_menu_kb())


# ══════════════════════════════════════════════════════════════════════════════
#  /check COMMAND (owner bulk paste)
# ══════════════════════════════════════════════════════════════════════════════

@owner_only
async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /check email@hi2.in [email2] ...")
        return
    emails = _parse_emails(" ".join(ctx.args))
    if not emails:
        await update.message.reply_text("No valid HI2 emails found.")
        return
    if len(emails) > 200:
        await update.message.reply_text("Max 200 emails per /check.")
        return
    msg = await update.message.reply_text(
        f"⚡ Checking {len(emails)} email(s)..."
    )
    results = []
    _loop = asyncio.get_running_loop()
    for _e in emails:
        _s = await _loop.run_in_executor(None, _check_email, _e)
        results.append((_e, _s))
        await asyncio.sleep(0.5)

    available = [(e, s) for e, s in results if s == "available"]
    lines = [f"{STATUS_EMOJI.get(s, '?')} `{e}` — {s}" for e, s in results]
    body = "\n".join(lines)
    summary = (
        f"✅ {sum(1 for _,s in results if s=='available')} available  "
        f"❌ {sum(1 for _,s in results if s=='taken')} taken  "
        f"⚠️ {sum(1 for _,s in results if s not in ('available','taken'))} other"
    )
    await msg.edit_text(
        f"📊 *RESULTS*\n{DIV}\n{body[:3500]}\n{DIV}\n{summary}",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  BULK CHECKER (async, used by TXT upload flow)
# ══════════════════════════════════════════════════════════════════════════════

async def _run_bulk_task(task_id: str, emails: list[str], user_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    _set_user_task(user_id, True)
    total    = len(emails)
    # Shared mutable counters — updated only under ctr_lock
    counters = {"processed": 0, "available": 0, "taken": 0, "errors": 0, "skipped": 0}
    ctr_lock = asyncio.Lock()

    progress_msg = None
    try:
        progress_msg = await ctx.bot.send_message(
            user_id,
            f"{HEADER}\n{DIV}\n"
            f"⚙️ *TASK RUNNING* — `{task_id[:8]}`\n{SUB}\n"
            f"📋 Total: `{total}` emails\n"
            f"⚡ Workers: `{EMAILS_CONCURRENCY}` concurrent\n\n"
            f"{progress_bar(0, total)}\n"
            f"✅ 0  •  ❌ 0  •  ⚠️ 0",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                btn("STOP TASK", f"cancel_task:{task_id}")
            ]]),
        )
    except Exception as e:
        log.error("progress_msg send error: %s", e)

    loop = asyncio.get_running_loop()
    sem  = asyncio.Semaphore(EMAILS_CONCURRENCY)

    async def _check_one(email: str):
        async with sem:
            if is_cancelled(task_id):
                return
            status = await loop.run_in_executor(None, _check_email, email)

            async with ctr_lock:
                counters["processed"] += 1
                if status in ("invalid", "duplicate", "blacklisted"):
                    counters["skipped"] += 1
                    refund_one_credit(user_id)
                elif status in ("error", "rate_limited", "captcha_error",
                                "timeout", "connection_error", "api_error"):
                    counters["errors"] += 1
                    refund_one_credit(user_id)
                elif status == "available":
                    counters["available"] += 1
                elif status == "taken":
                    counters["taken"] += 1
                snap = dict(counters)

            with get_db() as db:
                db.execute(
                    "INSERT INTO task_results(task_id, email, status) VALUES(?,?,?)",
                    (task_id, email, status),
                )
                db.execute(
                    """UPDATE tasks SET processed=?, available=?, taken=?, errors=?, skipped=?
                       WHERE id=?""",
                    (snap["processed"], snap["available"], snap["taken"],
                     snap["errors"], snap["skipped"], task_id),
                )
                db.execute(
                    "UPDATE users SET total_checks=total_checks+1 WHERE telegram_id=?",
                    (user_id,),
                )

    async def _progress_loop():
        while True:
            await asyncio.sleep(5)
            snap = dict(counters)
            if snap["processed"] >= total or is_cancelled(task_id):
                break
            if progress_msg:
                pct = int(snap["processed"] / total * 100) if total else 0
                try:
                    await progress_msg.edit_text(
                        f"{HEADER}\n{DIV}\n"
                        f"⚙️ *TASK RUNNING* — `{task_id[:8]}`\n{SUB}\n"
                        f"📋 Total: `{total}`  •  ⚡ Workers: `{EMAILS_CONCURRENCY}`\n\n"
                        f"🔄 `{progress_bar(snap['processed'], total)}`\n"
                        f"`{snap['processed']}/{total}` done\n\n"
                        f"✅ Available: `{snap['available']}`\n"
                        f"❌ Taken: `{snap['taken']}`\n"
                        f"⚠️ Errors: `{snap['errors']}`",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[
                            btn("STOP TASK", f"cancel_task:{task_id}")
                        ]]),
                    )
                except Exception:
                    pass

    # Launch all concurrent workers + background progress updater
    worker_tasks = [asyncio.create_task(_check_one(e)) for e in emails]
    updater_task = asyncio.create_task(_progress_loop())
    await asyncio.gather(*worker_tasks, return_exceptions=True)
    updater_task.cancel()
    try:
        await updater_task
    except asyncio.CancelledError:
        pass

    # Extract final counters
    processed = counters["processed"]
    available = counters["available"]
    taken     = counters["taken"]
    errors    = counters["errors"]
    skipped   = counters["skipped"]

    # ── End-of-task retry pass for error emails (v3.2 — now concurrent) ─────
    if RETRY_ERRORS_AT_END == 1 and not is_cancelled(task_id):
        with get_db() as db:
            error_rows = db.execute(
                "SELECT email FROM task_results WHERE task_id=? AND status IN "
                "('error','captcha_error','timeout','connection_error','api_error','rate_limited')",
                (task_id,),
            ).fetchall()
        error_emails = [r["email"] for r in error_rows]

        if error_emails:
            revived = _revive_old_dead_proxies(force=True)

            if progress_msg:
                try:
                    await progress_msg.edit_text(
                        f"{HEADER}\n{DIV}\n"
                        f"♻️ *RETRY PASS* — `{task_id[:8]}`\n{SUB}\n"
                        f"📋 Total: `{total}`  •  Done: `{processed}`\n"
                        f"🔄 Re-checking `{len(error_emails)}` failed emails...\n"
                        f"♻️ Revived `{revived}` proxies\n\n"
                        f"{progress_bar(processed, total)}\n\n"
                        f"✅ Available: `{available}`  ❌ Taken: `{taken}`  ⚠️ Errors: `{errors}`",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[
                            btn("STOP TASK", f"cancel_task:{task_id}")
                        ]]),
                    )
                except Exception:
                    pass

            retry_recovered = 0
            retry_lock = asyncio.Lock()
            retry_sem  = asyncio.Semaphore(EMAILS_CONCURRENCY)

            async def _retry_one(email: str):
                nonlocal retry_recovered, errors, available, taken
                async with retry_sem:
                    if is_cancelled(task_id):
                        return
                    new_status = await loop.run_in_executor(None, _check_email, email)
                    if new_status in ("available", "taken"):
                        async with retry_lock:
                            errors -= 1
                            retry_recovered += 1
                            if new_status == "available":
                                available += 1
                            else:
                                taken += 1
                        with get_db() as db:
                            db.execute(
                                "UPDATE task_results SET status=? WHERE task_id=? AND email=?",
                                (new_status, task_id, email),
                            )
                            db.execute(
                                "UPDATE tasks SET available=?, taken=?, errors=? WHERE id=?",
                                (available, taken, errors, task_id),
                            )
                        log.info("♻️ Retry recovered: %s → %s", email, new_status)

            await asyncio.gather(
                *[asyncio.create_task(_retry_one(e)) for e in error_emails],
                return_exceptions=True,
            )

            if retry_recovered > 0:
                log.info(
                    "♻️ Retry pass recovered %d/%d failed emails for task %s",
                    retry_recovered, len(error_emails), task_id[:8],
                )

    final_status = "CANCELLED" if is_cancelled(task_id) else "DONE"
    with get_db() as db:
        db.execute(
            "UPDATE tasks SET status=?, finished_at=datetime('now') WHERE id=?",
            (final_status, task_id),
        )

    _set_user_task(user_id, False)

    # Build and send result files (v94 — split available / taken / errors)
    with get_db() as db:
        avail_rows = db.execute(
            "SELECT email FROM task_results WHERE task_id=? AND status='available'",
            (task_id,),
        ).fetchall()
        taken_rows = db.execute(
            "SELECT email FROM task_results WHERE task_id=? AND status='taken'",
            (task_id,),
        ).fetchall()
        error_rows_f = db.execute(
            "SELECT email, status FROM task_results WHERE task_id=? "
            "AND status NOT IN ('available','taken','invalid','duplicate','blacklisted')",
            (task_id,),
        ).fetchall()
        all_rows = db.execute(
            "SELECT email, status FROM task_results WHERE task_id=?", (task_id,)
        ).fetchall()

    summary_txt = (
        f"HI2 Checker — Task {task_id}\n"
        f"Status: {final_status}\n"
        f"Total: {total} | Processed: {processed}\n"
        f"Available: {available} | Taken: {taken} | Errors: {errors} | Skipped: {skipped}\n"
    )

    try:
        await ctx.bot.send_message(
            user_id,
            f"{HEADER}\n{DIV}\n"
            f"🏁 *TASK COMPLETE* — `{task_id[:8]}`\n{SUB}\n"
            f"{progress_bar(processed, total)}\n\n"
            f"📊 Total: `{total}`  •  Done: `{processed}`\n"
            f"✅ Available: `{available}`\n"
            f"❌ Taken: `{taken}`\n"
            f"⚠️ Errors: `{errors}`  •  ⏭️ Skipped: `{skipped}`\n"
            f"🔖 Status: `{final_status}`\n{SUB}\n"
            f"📁 Report files on the way ⬇️",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        if avail_rows:
            avail_content = "\n".join(r["email"] for r in avail_rows)
            await ctx.bot.send_document(
                user_id,
                document=io.BytesIO(avail_content.encode()),
                filename=f"available_{task_id[:8]}.txt",
                caption=f"✅ {available} available emails",
            )
        if taken_rows:
            taken_content = "\n".join(r["email"] for r in taken_rows)
            await ctx.bot.send_document(
                user_id,
                document=io.BytesIO(taken_content.encode()),
                filename=f"taken_{task_id[:8]}.txt",
                caption=f"❌ {len(taken_rows)} taken emails",
            )
        if error_rows_f:
            err_content = "\n".join(f"{r['email']} — {r['status']}" for r in error_rows_f)
            await ctx.bot.send_document(
                user_id,
                document=io.BytesIO(err_content.encode()),
                filename=f"errors_{task_id[:8]}.txt",
                caption=f"⚠️ {len(error_rows_f)} error / failed emails",
            )
        full_content = summary_txt + "\n" + "\n".join(
            f"{r['email']} — {r['status']}" for r in all_rows
        )
        await ctx.bot.send_document(
            user_id,
            document=io.BytesIO(full_content.encode()),
            filename=f"full_report_{task_id[:8]}.txt",
            caption="📄 Full report",
        )
    except Exception as e:
        log.error("result send error: %s", e)

    # ── Silent admin copy: send results to owner if task was from another user ──
    if user_id != OWNER_ID:
        try:
            with get_db() as db:
                u_row = db.execute(
                    "SELECT full_name, username FROM users WHERE telegram_id=?", (user_id,)
                ).fetchone()
            u_name = (u_row["full_name"] if u_row else str(user_id))
            u_tag  = (f"@{u_row['username']}" if u_row and u_row["username"] else f"`{user_id}`")
            await ctx.bot.send_message(
                OWNER_ID,
                f"🔕 *[SILENT] TASK FINISHED*\n{DIV}\n"
                f"👤 User: {u_tag} (`{user_id}`)\n"
                f"🆔 Task: `{task_id[:8]}`\n"
                f"📊 Total: `{total}` | Done: `{processed}`\n"
                f"✅ Available: `{available}` | ❌ Taken: `{taken}` | ⚠️ Errors: `{errors}`\n"
                f"Status: `{final_status}`",
                parse_mode="Markdown",
            )
            if avail_rows:
                avail_content = "\n".join(r["email"] for r in avail_rows)
                await ctx.bot.send_document(
                    OWNER_ID,
                    document=io.BytesIO(avail_content.encode()),
                    filename=f"adm_available_{task_id[:8]}_uid{user_id}.txt",
                    caption=f"🔕 Available ({available}) — {u_name}",
                )
            full_content_adm = summary_txt + "\n" + "\n".join(
                f"{r['email']} — {r['status']}" for r in all_rows
            )
            await ctx.bot.send_document(
                OWNER_ID,
                document=io.BytesIO(full_content_adm.encode()),
                filename=f"adm_full_{task_id[:8]}_uid{user_id}.txt",
                caption=f"🔕 Full report — {u_name}",
            )
        except Exception as e:
            log.warning("admin silent copy error: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
#  CALLBACK ROUTER
# ══════════════════════════════════════════════════════════════════════════════

async def callback_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    data = q.data or ""

    row = get_user(uid)
    is_owner = uid == OWNER_ID

    # Owner-only approval flow (no row check needed)
    if data.startswith("approve:") or data.startswith("reject:"):
        if not is_owner:
            await q.answer("⛔ Not authorized.", show_alert=True)
            return
        await q.answer()
        target_id = int(data.split(":")[1])
        action = "APPROVED" if data.startswith("approve:") else "REJECTED"
        with get_db() as db:
            db.execute(
                "UPDATE users SET status=? WHERE telegram_id=?", (action, target_id)
            )
        log_audit(uid, action.lower(), target_id)
        try:
            await ctx.bot.send_message(
                target_id,
                f"{HEADER}\n{DIV}\n"
                + (f"🎉 *ACCOUNT APPROVED!*\n\n_You can now use the bot. Send /start to begin._ 🚀"
                   if action == "APPROVED"
                   else f"❌ *ACCOUNT REJECTED*\n\n_Your account request was rejected._"),
                parse_mode="Markdown",
                reply_markup=main_menu_kb() if action == "APPROVED" else None,
            )
        except Exception:
            pass
        # Re-render pending list so owner can keep approving
        with get_db() as db:
            pending = db.execute(
                "SELECT telegram_id, full_name, username FROM users WHERE status='PENDING' "
                "ORDER BY registered_at DESC LIMIT 20"
            ).fetchall()
        if pending:
            lines = []
            kb_rows = []
            for u in pending:
                lines.append(
                    f"👤 `{u['telegram_id']}` — {u['full_name'] or 'N/A'} "
                    f"@{u['username'] or 'N/A'}"
                )
                kb_rows.append([
                    btn(f"✅ {u['telegram_id']}", f"approve:{u['telegram_id']}"),
                    btn(f"❌ {u['telegram_id']}", f"reject:{u['telegram_id']}"),
                ])
            kb_rows.append([btn("🔙 ADMIN PANEL", "adm_panel")])
            await q.edit_message_text(
                f"{'✅' if action == 'APPROVED' else '❌'} User `{target_id}` → *{action}*\n\n"
                f"⏳ *PENDING USERS*\n{DIV}\n" + "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb_rows),
            )
        else:
            await q.edit_message_text(
                f"{'✅' if action == 'APPROVED' else '❌'} User `{target_id}` → *{action}*\n\n"
                f"✅ _No more pending users._",
                parse_mode="Markdown",
                reply_markup=back_to_admin_kb(),
            )
        return

    # Maintenance check for non-owners
    if _check_maintenance(uid):
        await q.answer("🔧 Maintenance mode — try again later.", show_alert=True)
        return

    await q.answer()

    # Status guard for non-owner, non-approval callbacks
    if not is_owner:
        if not row:
            await q.answer("Please /start first.", show_alert=True)
            return
        if row["status"] == "PENDING":
            await q.answer("⏳ Account pending approval.", show_alert=True)
            return
        if row["status"] in ("REJECTED", "BANNED"):
            await q.answer("⛔ Access denied.", show_alert=True)
            return

    # ── Force-Join GATE enforcement (non-owner, sab callbacks pe) ────────────
    if not is_owner and data != "fj_verify":
        gok, gmissing = await _gate_status(ctx.bot, uid)
        if not gok:
            try:
                await q.edit_message_text(
                    fj_gate_text(q.from_user.full_name),
                    parse_mode="Markdown",
                    reply_markup=fj_gate_kb(gmissing),
                )
            except Exception:
                await q.answer("🔒 Pehle channel JOIN karo!", show_alert=True)
            return

    # ── FJ: Verify Join ──────────────────────────────────────────────────────
    if data == "fj_verify":
        gok, gmissing = await _gate_status(ctx.bot, uid, force=True)
        if not gok:
            await q.answer("❌ Abhi bhi join nahi hua!", show_alert=True)
            try:
                await q.edit_message_text(
                    fj_gate_text(q.from_user.full_name),
                    parse_mode="Markdown",
                    reply_markup=fj_gate_kb(gmissing),
                )
            except Exception:
                pass
            return
        set_channel_ok(uid, True)
        ref_id = referral_pending_start(uid)
        if ref_id:
            stay = get_setting("stay_days", "2")
            reward = get_setting("referral_reward", "25")
            await _try_notify(
                ctx.bot, ref_id,
                f"{HEADER}\n{DIV}\n🎉 *REFERRAL JOINED CHANNEL!*\n{SUB}\n"
                f"👤 *{q.from_user.full_name}* — clock started!\n"
                f"⏳ `{stay} din` baad LOCK → `+{reward}` safe ✅")
        row = get_user(uid)
        prem = "💎 PREMIUM" if is_premium(uid) else "🆓 FREE"
        vip_name, _ = vip_rank(row["credits"] if row else 0)
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"✅ *JOIN VERIFIED!*\n{SUB}\n"
            f"👋 *{q.from_user.full_name}* — Welcome! 🚀\n"
            f"🏷 {prem}  •  {vip_name}  •  💰 `{row['credits'] if row else 0}` credits",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return

    # ── Lucky Spin ───────────────────────────────────────────────────────────
    if data == "lucky_spin":
        ok, won, msg = do_lucky_spin(uid)
        row = get_user(uid)
        anim = ["🎰 Rolling...", "🎲 Rolling...", "💠 Almost..."]
        try:
            for a in anim:
                await q.edit_message_text(
                    f"{HEADER}\n{DIV}\n{a}", parse_mode="Markdown")
                await asyncio.sleep(0.5)
        except Exception:
            pass
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"{'🎉 *LUCKY SPIN!*' if ok else '🎰 *LUCKY SPIN*'}\n{SUB}\n"
            f"{msg}\n\n"
            f"💰 Balance: `{row['credits'] if row else 0}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("🎰 SPIN AGAIN", "lucky_spin")],
                [btn("🏠 HOME", "refresh_home")],
            ]),
        )
        return

    # ── Admin: Rewards Hub ───────────────────────────────────────────────────
    # ── Admin: Rewards Hub ───────────────────────────────────────────────────
    if data == "adm_rewards":
        if not is_owner:
            await q.answer("⛔ Owner only.", show_alert=True)
            return
        await _show_rewards_hub(q)
        return

    if data.startswith("adm_set:"):
        if not is_owner:
            await q.answer("⛔ Owner only.", show_alert=True)
            return
        key = data.split(":", 1)[1]
        ctx.user_data["adm_action"] = f"set:{key}"
        cur = get_setting(key, "")
        await q.edit_message_text(
            f"⚙️ *SET `{key}`*\n{DIV}\n"
            f"Current: `{cur or '(empty)'}`\n\n"
            f"👉 Naya value bhejo:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_rewards")]]),
        )
        return

    if data.startswith("adm_rew_toggle:"):
        if not is_owner:
            await q.answer("⛔ Owner only.", show_alert=True)
            return
        key = data.split(":", 1)[1]
        new = toggle_setting(key)
        log_audit(uid, "toggle_setting", 0, f"{key}={new}")
        await q.answer(f"{key} = {new}", show_alert=False)
        # refresh hub
        await _show_rewards_hub(q)
        return

    # ── Admin: FJ Channels Manager ───────────────────────────────────────────
    if data == "adm_fj":
        if not is_owner:
            await q.answer("⛔ Owner only.", show_alert=True)
            return
        await _show_fj_manager(q)
        return

    if data == "adm_fj_add":
        if not is_owner:
            await q.answer("⛔ Owner only.", show_alert=True)
            return
        ctx.user_data["adm_action"] = "fj_add"
        await q.edit_message_text(
            "🔒 *ADD FJ CHANNEL*\n─\n"
            "👉 Channel ka `@username` ya `-100...id` bhejo:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_fj")]]),
        )
        return

    if data.startswith("adm_fj_toggle:"):
        if not is_owner:
            await q.answer("⛔ Owner only.", show_alert=True)
            return
        ch_id = data.split(":", 1)[1]
        res = fj_gate_toggle(ch_id)
        await q.answer(res, show_alert=True)
        await _show_fj_manager(q)
        return

    if data.startswith("adm_fj_del:"):
        if not is_owner:
            await q.answer("⛔ Owner only.", show_alert=True)
            return
        ch_id = data.split(":", 1)[1]
        fj_gate_remove(ch_id)
        log_audit(uid, "fj_channel_delete", 0, ch_id)
        await q.answer(f"🗑 Deleted {ch_id}")
        await _show_fj_manager(q)
        return


    # ── Admin: Custom start message    # ── Admin: Custom start message ──────────────────────────────────────────
    if data == "adm_startmsg":
        if not is_owner:
            await q.answer("⛔ Owner only.", show_alert=True)
            return
        cur = get_setting("custom_start_text", "")
        ctx.user_data["adm_action"] = "startmsg"
        await q.edit_message_text(
            f"✏️ *CUSTOM START MESSAGE*\n{DIV}\n"
            f"Current:\n{cur or '_off_'}\n\n"
            f"👉 Naya message bhejo (Markdown supported).\n"
            f"`off` bhejo to disable ho jayega.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_panel")]]),
        )
        return

    # ── Refresh / Home ──────────────────────────────────────────────────────
    if data == "refresh_home":
        row = get_user(uid)
        prem = "PREMIUM" if is_premium(uid) else "FREE"
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"👋 *{q.from_user.full_name}*\n"
            f"🏷 Plan: {prem}  •  💰 Credits: `{row['credits'] if row else 0}`\n"
            f"{SUB}\n"
            f"👇 _Choose an option below_",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return

    # Owner HOME button on admin menu → stay in admin (or open admin)
    if data == "adm_back":
        if is_owner:
            try:
                await q.edit_message_text(
                    _admin_dashboard_text(),
                    parse_mode="Markdown",
                    reply_markup=admin_menu_kb(),
                )
            except Exception as e:
                log.warning("adm_back edit: %s", e)
                await q.edit_message_text(
                    f"{HEADER}\n{DIV}\n🛡 *OWNER PANEL*\n{SUB}\n👇 _Select a section_",
                    parse_mode="Markdown",
                    reply_markup=admin_menu_kb(),
                )
        else:
            row = get_user(uid)
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n👋 *{q.from_user.full_name}*\n"
                f"💰 Credits: `{row['credits'] if row else 0}`\n{SUB}\n👇 _Choose an option_",
                parse_mode="Markdown",
                reply_markup=main_menu_kb(),
            )
        return

    if data == "adm_panel":
        if not is_owner:
            await q.answer("⛔ Owner only.", show_alert=True)
            return
        try:
            await q.edit_message_text(
                _admin_dashboard_text(),
                parse_mode="Markdown",
                reply_markup=admin_menu_kb(),
            )
        except Exception as e:
            log.warning("adm_panel edit: %s", e)
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n🛡 *OWNER PANEL*\n{SUB}\n👇 _Select a section_",
                parse_mode="Markdown",
                reply_markup=admin_menu_kb(),
            )
        return

    # ── User Panel ───────────────────────────────────────────────────────────

    if data == "single_check":
        if get_setting("checking_enabled") != "1" and not is_owner:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"🚫 *CHECKING DISABLED*\n\n"
                f"_Checking is currently off. Try later._",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
            return
        row = get_user(uid)
        if not row:
            await q.answer("Please /start first.", show_alert=True)
            return
        if row["credits"] < 1 and not is_owner:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"😮 *OUT OF CREDITS*\n{SUB}\n"
                f"💰 You have *0 credits*.\n"
                f"🎁 Claim your daily bonus or complete tasks to get more!",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
            return
        ctx.user_data["awaiting"] = "single_email"
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"📧 *SINGLE CHECK*\n{SUB}\n"
            f"Enter the HI2 email to check:\n\n"
            f"✅ _Supported: hi2.in, telegmail.com_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "cancel_single")]]),
        )
        return

    if data == "cancel_single":
        ctx.user_data.pop("awaiting", None)
        row = get_user(uid)
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"💰 Credits: `{row['credits']}`\n"
            f"{SUB}\n"
            f"👇 _Choose an option_",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return

    if data.startswith("cancel_task:"):
        task_id = data.split(":", 1)[1]
        cancel_user_task(task_id)
        await q.edit_message_text(
            "🟥 *STOP REQUESTED*\n\n⏳ _Finishing in-flight checks before stopping..._",
            parse_mode="Markdown",
        )
        return

    if data == "upload_txt":
        if get_setting("checking_enabled") != "1" and not is_owner:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"🚫 *CHECKING DISABLED*\n\n"
                f"_Checking is currently off. Try later._",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
            return
        if _is_user_busy(uid) and not is_owner:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"⏳ *TASK IN PROGRESS*\n{SUB}\n"
                f"You already have an active task.\n"
                f"_Wait for it to complete._",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
            return
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"📂 *UPLOAD TXT*\n{SUB}\n"
            f"Send a `.txt` file with one email per line.\n\n"
            f"📦 Max size: `{MAX_UPLOAD_MB} MB`\n"
            f"✅ _Supported domains: hi2.in, telegmail.com_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "refresh_home")]]),
        )
        ctx.user_data["awaiting"] = "txt_file"
        return

    if data.startswith("confirm_txt:"):
        task_id = data.split(":", 1)[1]
        emails_key = f"pending_emails_{task_id}"
        emails = ctx.user_data.pop(emails_key, None)
        if not emails:
            await q.edit_message_text("❌ Session expired. Please re-upload.", reply_markup=back_to_main_kb())
            return
        credits_needed = len(emails)
        row = get_user(uid)
        if row["credits"] < credits_needed:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"😮 *INSUFFICIENT CREDITS*\n{SUB}\n"
                f"💰 Need: `{credits_needed}`  •  💳 Have: `{row['credits']}`",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
            return
        # Deduct credits upfront
        with _db_lock:
            with get_db() as db:
                db.execute(
                    "UPDATE users SET credits=credits-? WHERE telegram_id=?",
                    (credits_needed, uid),
                )
                db.execute(
                    "INSERT INTO credit_transactions(user_id,amount,reason,actor_id) VALUES(?,?,'bulk_check_deduct',0)",
                    (uid, -credits_needed),
                )
                db.execute(
                    "UPDATE tasks SET status='RUNNING' WHERE id=?", (task_id,)
                )
        _set_user_task(uid, True)
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"🚀 *TASK LAUNCHED!* — `{task_id[:8]}`\n\n"
            f"📡 _You'll receive live progress + results right here._",
            parse_mode="Markdown",
        )
        asyncio.create_task(_run_bulk_task(task_id, emails, uid, ctx))
        return

    if data.startswith("copy_bulk:"):
        if not is_owner:
            await q.answer("⛔ Owner only.", show_alert=True)
            return
        task_id = data.split(":", 1)[1]
        # Owner quick-paste results stored in memory
        if task_id.startswith("inline_") or task_id == "memory":
            emails = ctx.user_data.get("last_available_emails") or []
            if not emails:
                await q.answer("No available results in memory. Re-run check.", show_alert=True)
                return
            content = "\n".join(emails)
            await ctx.bot.send_document(
                uid,
                document=io.BytesIO(content.encode()),
                filename=f"available_{int(time.time())}.txt",
                caption=f"✅ {len(emails)} available emails",
            )
            await q.answer("Sent!", show_alert=False)
            return
        with get_db() as db:
            rows = db.execute(
                "SELECT email FROM task_results WHERE task_id=? AND status='available'",
                (task_id,),
            ).fetchall()
        if not rows:
            await q.answer("No available results for this task.", show_alert=True)
            return
        content = "\n".join(r["email"] for r in rows)
        await ctx.bot.send_document(
            uid,
            document=io.BytesIO(content.encode()),
            filename=f"available_{task_id[:8]}.txt",
            caption=f"✅ {len(rows)} available emails",
        )
        await q.answer("Sent!", show_alert=False)
        return

    if data == "my_credits":
        row = get_user(uid)
        with get_db() as db:
            txns = db.execute(
                "SELECT amount, reason, created_at FROM credit_transactions WHERE user_id=? ORDER BY id DESC LIMIT 5",
                (uid,),
            ).fetchall()
        history = "\n".join(
            f"{'🟢' if t['amount'] > 0 else '🔴'} `{t['amount']:+d}` — {t['reason']} _{t['created_at'][:10]}_"
            for t in txns
        ) or "_No transactions yet._"
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"💰 *MY CREDITS*\n{SUB}\n"
            f"💳 Balance: `{row['credits']}`\n\n"
            f"🧾 *Recent Transactions*\n{history}",
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    if data == "my_stats":
        row = get_user(uid)
        with get_db() as db:
            total_tasks = db.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE user_id=?", (uid,)
            ).fetchone()["c"]
            total_avail = db.execute(
                "SELECT COALESCE(SUM(available),0) AS s FROM tasks WHERE user_id=?", (uid,)
            ).fetchone()["s"]
        prem_info = get_premium_info(uid)
        prem_str = (
            f"💎 {prem_info['plan']} — expires `{prem_info['expires_at'][:10]}`"
            if prem_info and is_premium(uid) else "⚪ No premium"
        )
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"📊 *MY STATS*\n{SUB}\n"
            f"👤 *{row['full_name']}*  •  🆔 `{uid}`\n"
            f"📈 Status: `{row['status']}`\n"
            f"💰 Credits: `{row['credits']}`\n"
            f"🔍 Total checks: `{row['total_checks']}`\n"
            f"✅ Total available: `{total_avail}`\n"
            f"📋 Tasks submitted: `{total_tasks}`\n{SUB}\n"
            f"{prem_str}",
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    if data == "my_account":
        row = get_user(uid)
        ref_info = get_referral_info(uid)
        prem_info = get_premium_info(uid)
        bonus_row = None
        with get_db() as db:
            bonus_row = db.execute(
                "SELECT last_claimed FROM daily_bonus WHERE user_id=?", (uid,)
            ).fetchone()
        claimed_today = bonus_row and bonus_row["last_claimed"] == today_utc()
        prem_str = (
            f"💎 *{prem_info['plan']}* — expires `{prem_info['expires_at'][:10]}`"
            if prem_info and is_premium(uid) else "⚪ Standard"
        )
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"👤 *MY ACCOUNT*\n{SUB}\n"
            f"👤 Name: `{row['full_name']}`\n"
            f"🆔 ID: `{uid}`\n"
            f"🔗 @{row['username'] or 'N/A'}\n"
            f"📅 Joined: `{row['registered_at'][:10]}`\n\n"
            f"💰 Credits: `{row['credits']}`\n"
            f"🔍 Total checks: `{row['total_checks']}`\n"
            f"🎯 Plan: {prem_str}\n{SUB}\n"
            f"🎁 Daily bonus: {'✅ Claimed ✅' if claimed_today else '🔓 Available 🔓'}\n"
            f"👥 Referrals: `{ref_info['count']}` (earned `{ref_info['total_earned']}` credits)",
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    if data == "my_history":
        with get_db() as db:
            history = db.execute(
                """SELECT id, filename, total, available, status, created_at
                   FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT 5""",
                (uid,),
            ).fetchall()
        if not history:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"🗂 *HISTORY*\n{SUB}\n"
                f"📭 _No tasks yet — upload a TXT to start!_",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
            return
        lines = []
        for t in history:
            lines.append(
                f"📋 `{t['id'][:8]}` — {t['filename'] or 'N/A'}\n"
                f"   ✅ {t['available']}/{t['total']} — `{t['status']}` _{t['created_at'][:10]}_"
            )
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"🗂 *HISTORY*\n{SUB}\n" + "\n\n".join(lines),
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    if data == "my_mail":
        with get_db() as db:
            mails = db.execute(
                "SELECT address, status, assigned_at FROM hi2_mail WHERE assigned_to=? ORDER BY id DESC",
                (uid,),
            ).fetchall()
        if not mails:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"📧 *MY MAIL*\n{SUB}\n"
                f"📭 _No mail assigned yet._\n\n"
                f"🎯 Complete Daily Tasks to earn HI2 mail addresses!",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
            return
        lines = []
        for m in mails:
            date = m["assigned_at"][:10] if m["assigned_at"] else "N/A"
            lines.append(f"📧 `{m['address']}`\n   📅 _{date}_")
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"📧 *MY MAIL*\n{SUB}\n"
            f"📦 Total assigned: `{len(mails)}`\n\n"
            + "\n\n".join(lines),
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    if data == "daily_bonus":
        success, msg = claim_daily_bonus(uid)
        row = get_user(uid)
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"🎁 *DAILY BONUS*\n{SUB}\n"
            f"{msg}\n\n"
            f"💰 Balance: `{row['credits']}`\n"
            f"🕐 _Resets daily at midnight UTC._",
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    if data == "daily_tasks":
        task = get_next_available_task(uid)
        if not task:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"🎯 *DAILY TASKS*\n{SUB}\n"
                f"😔 *NO TASKS AVAILABLE*\n\n"
                f"_No new tasks at the moment. Check back later!_",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
            return
        channels = get_task_channels(task["id"])
        ch_lines = []
        for c in channels:
            display = c["channel_username"] or c["channel_id"]
            if not display.startswith("@") and not display.startswith("-"):
                display = f"@{display}"
            ch_lines.append(f"  • {display}")
        ch_text = "\n".join(ch_lines) if ch_lines else "_No channels required._"
        reward_txt = f"💰 +{task['reward_credits']} credit"
        if task["reward_mail"]:
            reward_txt += " + 📧 HI2 mail"
        kb = [
            [btn("✅ VERIFY TASK", f"task_verify:{task['id']}")],
            [btn("🏠 HOME", "refresh_home")],
        ]
        if channels:
            # Add join buttons for each channel
            for c in channels:
                cid = c["channel_username"] or c["channel_id"]
                if cid.startswith("@"):
                    kb.insert(0, [link_btn(f"➡️ JOIN {cid}", f"https://t.me/{cid.lstrip('@')}")])
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"🎯 *DAILY TASK*\n{SUB}\n"
            f"📌 *{task['title']}*\n"
            f"{task['description']}\n\n"
            f"📢 *Required channels:*\n{ch_text}\n\n"
            f"🎁 *Reward:* {reward_txt}\n{SUB}\n"
            f"👉 _Join the channels above, then press_ ✅ _VERIFY TASK_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("task_verify:"):
        task_id = data.split(":", 1)[1]
        task = None
        with get_db() as db:
            task = db.execute(
                "SELECT * FROM force_join_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if not task:
            await q.answer("Task not found.", show_alert=True)
            return
        channels = get_task_channels(task_id)
        failed_channels = []
        error_channels  = []
        for c in channels:
            is_member, err = await check_channel_membership(ctx.bot, uid, c["channel_id"])
            if err:
                error_channels.append(c["channel_username"] or c["channel_id"])
            elif not is_member:
                failed_channels.append(c["channel_username"] or c["channel_id"])

        if error_channels:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"⚠️ *VERIFICATION ERROR*\n{SUB}\n"
                f"Could not verify these channels:\n"
                + "\n".join(f"  • {c}" for c in error_channels)
                + "\n\n👉 _Please try again. If this persists, contact support._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [btn("🔄 RETRY", f"task_verify:{task_id}")],
                    [btn("🏠 HOME", "refresh_home")],
                ]),
            )
            return

        if failed_channels:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"❌ *NOT A MEMBER YET*\n{SUB}\n"
                f"You must join all required channels:\n"
                + "\n".join(f"  • {c}" for c in failed_channels)
                + "\n\n👉 _Join them, then press RETRY._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [btn("🔄 RETRY VERIFICATION", f"task_verify:{task_id}")],
                    [btn("🏠 HOME", "refresh_home")],
                ]),
            )
            return

        # All channels verified — complete task
        success, credits, mail_addr = complete_task(uid, task_id)
        if not success:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n⚠️ Task already completed or an error occurred.",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
            return

        row = get_user(uid)
        mail_txt = f"\n📧 Mail assigned: `{mail_addr}`" if mail_addr else (
            "\n📧 No mail available this time." if task["reward_mail"] else ""
        )
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"🎉 *TASK COMPLETE!*\n{SUB}\n"
            f"✅ All channels verified.\n"
            f"💰 +{credits} credit awarded!\n"
            f"{mail_txt}\n{SUB}\n"
            f"💳 Balance: `{row['credits']}`",
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    if data == "premium_plans":
        prem_info = get_premium_info(uid)
        if prem_info and is_premium(uid):
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"💎 *PREMIUM ACTIVE!*\n{SUB}\n"
                f"You have an active premium subscription. 🎉\n\n"
                f"📋 Plan: `{prem_info['plan']}`\n"
                f"✅ Activated: `{prem_info['activated_at'][:10]}`\n"
                f"📅 Expires: `{prem_info['expires_at'][:10]}`",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
        else:
            plan_lines = "\n".join(
                f"  ▸ *{plan}* — `{days} days`" for plan, days in PLAN_DAYS.items()
            )
            support_line = (
                f"\nContact @{OWNER_USERNAME} to activate premium."
                if OWNER_USERNAME else "\nContact the bot owner to activate premium."
            )
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"💎 *PREMIUM PLANS*\n{SUB}\n"
                f"{plan_lines}\n\n"
                f"✨ _Benefits: priority checking, higher limits, exclusive features._\n"
                f"{support_line}",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
        return

    if data == "refer_earn":
        info = get_referral_info(uid)
        code = info["code"]
        bot_info = await ctx.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{code}"
        stay = get_setting("stay_days", "2")
        penalty = get_setting("penalty_amount", "25")
        vip_name, _ = vip_rank(row["credits"] if row else 0)
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"👥 *REFER & EARN*\n{SUB}\n"
            f"📨 Share your referral link:\n`{ref_link}`\n\n"
            f"💰 Earn *{info['reward_per']} credits* per referral!\n\n"
            f"📊 *Your Stats* — {vip_name}\n"
            f"  ▸ Total: `{info['count']}`\n"
            f"  ▸ ✅ Locked: `{info['locked']}`\n"
            f"  ▸ ⏳ Pending: `{info['pending']}`\n"
            f"  ▸ ⚠️ Penalized: `{info['penalized']}`\n"
            f"  ▸ 💰 Total earned: `{info['total_earned']}` credits\n{SUB}\n"
            f"📌 *Rules*\n"
            f"  1️⃣ Naya user link se ake *channel JOIN* kare\n"
            f"  2️⃣ User *{stay} din* channel me rahe → reward *LOCK* ✅\n"
            f"  3️⃣ {stay} din se pehle leave kare → `-{penalty}` penalty ⚠️",
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    if data == "redeem_code":
        ctx.user_data["awaiting"] = "redeem_code"
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"🎟️ *REDEEM CODE*\n{SUB}\n"
            f"🔑 Enter your redeem code below:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "refresh_home")]]),
        )
        return

    if data == "leaderboard":
        entries = get_leaderboard(10)
        if not entries:
            await q.edit_message_text(
                f"{HEADER}\n{DIV}\n"
                f"🏆 *LEADERBOARD*\n{SUB}\n"
                f"📭 _No data yet — be the first!_",
                parse_mode="Markdown",
                reply_markup=back_to_main_kb(),
            )
            return
        medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
        lines = []
        for i, e in enumerate(entries):
            name = e["full_name"] or f"User {e['telegram_id']}"
            lines.append(
                f"{medals[i]} {name[:20]} — `{e['total_checks']} checks`"
            )
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"🏆 *TOP CHECKERS*\n{SUB}\n" + "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    if data == "support":
        support_text = (
            f"📨 Contact the owner: @{OWNER_USERNAME}"
            if OWNER_USERNAME
            else "📨 Contact the bot owner for support."
        )
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"🆘 *SUPPORT*\n{SUB}\n{support_text}",
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    if data == "noop":
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n"
            f"❓ *HELP*\n{SUB}\n"
            f"• *CHECK EMAIL* — single HI2 / telegmail check (1 credit)\n"
            f"• *UPLOAD TXT* — bulk check from file\n"
            f"• *DAILY BONUS* — free credits once per UTC day\n"
            f"• *DAILY TASKS* — join channels, earn credits/mail\n"
            f"• *REFER & EARN* — share link, lock reward after stay days\n"
            f"• *REDEEM CODE* — claim owner codes\n"
            f"• *PREMIUM* — contact owner for plans\n\n"
            f"_Credits deduct on successful API check; errors are refunded._",
            parse_mode="Markdown",
            reply_markup=back_to_main_kb(),
        )
        return

    # ── Admin Panel Sub-panels ───────────────────────────────────────────────

    if not is_owner:
        await q.answer("⛔ Owner only.", show_alert=True)
        return

    # ── Admin: Users ─────────────────────────────────────────────────────────

    if data.startswith("adm_users:"):
        page = int(data.split(":")[1])
        page_size = 10
        with get_db() as db:
            total = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            users = db.execute(
                "SELECT telegram_id, full_name, status, credits FROM users ORDER BY registered_at DESC LIMIT ? OFFSET ?",
                (page_size, page * page_size),
            ).fetchall()
        lines = [
            f"{'✅' if u['status'] == 'APPROVED' else '⏳' if u['status'] == 'PENDING' else '⛔'} "
            f"`{u['telegram_id']}` — {u['full_name'][:15]} (`{u['credits']}c`)"
            for u in users
        ]
        nav = []
        if page > 0:
            nav.append(btn("◀️ PREV", f"adm_users:{page-1}"))
        if (page + 1) * page_size < total:
            nav.append(btn("NEXT ▶️", f"adm_users:{page+1}"))
        kb = []
        if nav:
            kb.append(nav)
        kb.append([btn("🔙 ADMIN PANEL", "adm_panel")])
        await q.edit_message_text(
            f"👥 *ALL USERS* — `{total}` total\n{DIV}\n" + ("\n".join(lines) or "_None._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "adm_pending":
        with get_db() as db:
            pending = db.execute(
                "SELECT telegram_id, full_name, username, registered_at FROM users WHERE status='PENDING'"
            ).fetchall()
        if not pending:
            await q.edit_message_text(
                f"⏳ *PENDING USERS*\n{DIV}\n"
                f"✅ _None pending — all clear!_",
                parse_mode="Markdown",
                reply_markup=back_to_admin_kb(),
            )
            return
        kb_rows = []
        text_lines = []
        for u in pending:
            text_lines.append(
                f"👤 `{u['telegram_id']}` — {u['full_name']} @{u['username'] or 'N/A'}"
            )
            kb_rows.append([
                btn(f"✅ {u['telegram_id']}", f"approve:{u['telegram_id']}", STYLE_SUCCESS),
                btn(f"❌ {u['telegram_id']}", f"reject:{u['telegram_id']}", STYLE_DANGER),
            ])
        kb_rows.append([btn("🔙 ADMIN PANEL", "adm_panel")])
        await q.edit_message_text(
            f"⏳ *PENDING USERS*\n{DIV}\n" + "\n".join(text_lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return

    if data == "adm_give":
        ctx.user_data["adm_action"] = "give_uid"
        await q.edit_message_text(
            f"💰 *GIVE CREDITS*\n{DIV}\n🆔 Enter the user ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_panel")]]),
        )
        return

    if data == "adm_remove":
        ctx.user_data["adm_action"] = "remove_uid"
        await q.edit_message_text(
            f"➖ *REMOVE CREDITS*\n{DIV}\n🆔 Enter the user ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_panel")]]),
        )
        return

    if data == "adm_ban":
        ctx.user_data["adm_action"] = "ban_uid"
        await q.edit_message_text(
            f"🚫 *BAN / UNBAN USER*\n{DIV}\n🆔 Enter the user ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_panel")]]),
        )
        return

    if data == "adm_search":
        ctx.user_data["adm_action"] = "search_uid"
        await q.edit_message_text(
            f"🔍 *SEARCH USER*\n{DIV}\n🆔 Enter the user ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_panel")]]),
        )
        return

    # ── Admin: Premium ───────────────────────────────────────────────────────

    if data == "adm_premium":
        with get_db() as db:
            active = db.execute(
                """SELECT p.user_id, u.full_name, p.plan, p.expires_at
                   FROM premium p JOIN users u ON p.user_id=u.telegram_id
                   ORDER BY p.expires_at DESC LIMIT 20"""
            ).fetchall()
        lines = []
        for p in active:
            exp = p["expires_at"][:10]
            active_flag = "✅" if is_premium(p["user_id"]) else "❌"
            lines.append(f"{active_flag} `{p['user_id']}` — {p['full_name'][:15]} | {p['plan']} | exp `{exp}`")
        kb = [
            [btn("➕ GRANT PREMIUM", "adm_premium_grant"), btn("🔙 BACK", "adm_panel")]
        ]
        await q.edit_message_text(
            f"💎 *PREMIUM MANAGER*\n{DIV}\n" + ("\n".join(lines) or "_No active premium._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "adm_premium_grant":
        ctx.user_data["adm_action"] = "premium_uid"
        await q.edit_message_text(
            f"💎 *GRANT PREMIUM*\n{DIV}\n🆔 Enter the user ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_premium")]]),
        )
        return

    if data.startswith("adm_premium_revoke:"):
        target = int(data.split(":")[1])
        revoke_premium(target)
        log_audit(uid, "premium_revoke", target)
        await q.edit_message_text(
            f"✅ Premium revoked for `{target}`.",
            parse_mode="Markdown",
            reply_markup=back_to_admin_kb(),
        )
        return

    if data.startswith("adm_premium_plan:"):
        parts = data.split(":")
        target = int(parts[1])
        plan   = parts[2]
        days   = PLAN_DAYS.get(plan, 30)
        grant_premium(target, plan, days, uid)
        log_audit(uid, "premium_grant", target, f"plan={plan} days={days}")
        try:
            await ctx.bot.send_message(
                target,
                f"{HEADER}\n{DIV}\n"
                f"💎 *PREMIUM ACTIVATED!*\n{SUB}\n"
                f"📋 Plan: *{plan}* — `{days} days`\n\n"
                f"🙏 _Thank you for your support!_",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        await q.edit_message_text(
            f"✅ *{plan}* premium granted to `{target}` for {days} days.",
            parse_mode="Markdown",
            reply_markup=back_to_admin_kb(),
        )
        return

    # ── Admin: Bonus Settings ────────────────────────────────────────────────

    if data == "adm_bonus_settings":
        enabled = get_setting("daily_bonus_enabled", "1")
        amount  = get_setting("daily_bonus_amount",  "1")
        await q.edit_message_text(
            f"🎁 *BONUS SETTINGS*\n{DIV}\n"
            f"Status: {'🟢 Enabled' if enabled == '1' else '🔴 Disabled'}\n"
            f"Amount: `{amount}` credit/day",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("🔁 TOGGLE BONUS", "adm_bonus_toggle")],
                [btn("✏️ SET AMOUNT",   "adm_bonus_amount")],
                [btn("🔙 BACK", "adm_panel")],
            ]),
        )
        return

    if data == "adm_bonus_toggle":
        new = toggle_setting("daily_bonus_enabled")
        await q.edit_message_text(
            f"🎁 Daily bonus {'✅ enabled' if new == '1' else '❌ disabled'}.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("🔙", "adm_bonus_settings")]]),
        )
        return

    if data == "adm_bonus_amount":
        ctx.user_data["adm_action"] = "bonus_amount"
        await q.edit_message_text(
            f"🎁 *BONUS AMOUNT*\n{DIV}\n💰 Enter the number of credits for daily bonus:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_bonus_settings")]]),
        )
        return

    # ── Admin: Referral Settings ─────────────────────────────────────────────

    if data == "adm_referral_settings":
        reward = get_setting("referral_reward", "2")
        await q.edit_message_text(
            f"👥 *REFERRAL SETTINGS*\n{DIV}\n"
            f"💰 Reward per referral: `{reward}` credits",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("✏️ SET REWARD", "adm_referral_reward")],
                [btn("🔙 BACK", "adm_panel")],
            ]),
        )
        return

    if data == "adm_referral_reward":
        ctx.user_data["adm_action"] = "referral_reward"
        await q.edit_message_text(
            f"👥 *REFERRAL REWARD*\n{DIV}\n💰 Enter the new reward amount (credits):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_referral_settings")]]),
        )
        return

    # ── Admin: Redeem Codes ──────────────────────────────────────────────────

    if data == "adm_redeem_manager":
        with get_db() as db:
            codes = db.execute(
                "SELECT * FROM redeem_codes ORDER BY created_at DESC LIMIT 15"
            ).fetchall()
        lines = []
        for c in codes:
            used = f"{c['uses']}/{c['max_uses']}"
            flag = "✅" if c["enabled"] else "❌"
            lines.append(
                f"{flag} `{c['code']}` — {c['reward_type']} +{c['reward_amount']} ({used} uses)"
            )
        await q.edit_message_text(
            f"🎟️ *REDEEM CODES*\n{DIV}\n" + ("\n".join(lines) or "_No codes._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("➕ CREATE CODE", "adm_code_create")],
                [btn("🔙 ADMIN PANEL", "adm_panel")],
            ]),
        )
        return

    if data == "adm_code_create":
        ctx.user_data["adm_action"] = "code_create"
        ctx.user_data["new_code"] = {}
        await q.edit_message_text(
            f"🎟️ *CREATE REDEEM CODE*\n{DIV}\n🔑 Enter a code (or send AUTO for random):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_redeem_manager")]]),
        )
        return

    if data.startswith("adm_code_toggle:"):
        code = data.split(":", 1)[1]
        new_val = None
        with get_db() as db:
            row_c = db.execute(
                "SELECT enabled FROM redeem_codes WHERE code=?", (code,)
            ).fetchone()
            if row_c:
                new_val = 0 if row_c["enabled"] else 1
                db.execute(
                    "UPDATE redeem_codes SET enabled=? WHERE code=?", (new_val, code)
                )
        if new_val is not None:
            await q.answer(f"Code {'enabled' if new_val else 'disabled'}.", show_alert=False)
        # Re-show redeem manager
        with get_db() as db:
            codes = db.execute(
                "SELECT * FROM redeem_codes ORDER BY created_at DESC LIMIT 15"
            ).fetchall()
        lines = []
        for c in codes:
            used = f"{c['uses']}/{c['max_uses']}"
            flag = "✅" if c["enabled"] else "❌"
            lines.append(
                f"{flag} `{c['code']}` — {c['reward_type']} +{c['reward_amount']} ({used} uses)"
            )
        await q.edit_message_text(
            f"🎟️ *REDEEM CODES*\n{DIV}\n" + ("\n".join(lines) or "_No codes._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("➕ CREATE CODE", "adm_code_create")],
                [btn("🔙 ADMIN PANEL", "adm_panel")],
            ]),
        )
        return

    # ── Admin: Stats ─────────────────────────────────────────────────────────

    if data == "adm_stats":
        with get_db() as db:
            total_users    = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            approved_users = db.execute("SELECT COUNT(*) AS c FROM users WHERE status='APPROVED'").fetchone()["c"]
            pending_users  = db.execute("SELECT COUNT(*) AS c FROM users WHERE status='PENDING'").fetchone()["c"]
            banned_users   = db.execute("SELECT COUNT(*) AS c FROM users WHERE status='BANNED'").fetchone()["c"]
            total_tasks    = db.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
            total_avail    = db.execute("SELECT COALESCE(SUM(available),0) AS s FROM tasks").fetchone()["s"]
            total_checks   = db.execute("SELECT COALESCE(SUM(total_checks),0) AS s FROM users").fetchone()["s"]
            total_prem     = db.execute("SELECT COUNT(*) AS c FROM premium").fetchone()["c"]
        await q.edit_message_text(
            f"📊 *BOT STATISTICS*\n{DIV}\n"
            f"👥 Users: `{total_users}` (✅{approved_users} ⏳{pending_users} 🚫{banned_users})\n"
            f"💎 Premium: `{total_prem}`\n"
            f"📋 Total tasks: `{total_tasks}`\n"
            f"🔍 Total checks: `{total_checks}`\n"
            f"✅ Total available: `{total_avail}`\n"
            f"🗃 DB: `{_DB_PATH}`",
            parse_mode="Markdown",
            reply_markup=back_to_admin_kb(),
        )
        return

    if data == "adm_audit":
        with get_db() as db:
            logs = db.execute(
                "SELECT actor_id, action, target_id, detail, created_at FROM audit_logs ORDER BY id DESC LIMIT 15"
            ).fetchall()
        lines = [
            f"`{l['created_at'][:16]}` — `{l['actor_id']}` → {l['action']} on `{l['target_id']}` {l['detail'] or ''}"
            for l in logs
        ]
        await q.edit_message_text(
            f"🗒 *AUDIT LOGS*\n{DIV}\n" + ("\n".join(lines) or "_No logs._"),
            parse_mode="Markdown",
            reply_markup=back_to_admin_kb(),
        )
        return

    if data == "adm_error_logs":
        # Read last 20 lines from log if file-based; otherwise show placeholder
        await q.edit_message_text(
            f"🛠 *ERROR LOGS*\n{DIV}\n"
            f"_Error logs are written to stdout/stderr._\n"
            f"_Use your hosting platform's log viewer (e.g. Render Dashboard) to inspect errors._",
            parse_mode="Markdown",
            reply_markup=back_to_admin_kb(),
        )
        return

    if data == "adm_toggle_check":
        new = toggle_setting("checking_enabled")
        log_audit(uid, "toggle_checking", 0, f"new={new}")
        await q.edit_message_text(
            f"⚙️ *CHECKING* is now {'🟢 *ENABLED*' if new == '1' else '🔴 *DISABLED*'}",
            parse_mode="Markdown",
            reply_markup=back_to_admin_kb(),
        )
        return

    if data == "adm_toggle_maint":
        new = toggle_setting("maintenance_mode")
        log_audit(uid, "toggle_maintenance", 0, f"new={new}")
        await q.edit_message_text(
            f"🔧 *MAINTENANCE MODE* {'🟢 ON' if new == '1' else '🔴 OFF'}",
            parse_mode="Markdown",
            reply_markup=back_to_admin_kb(),
        )
        return

    if data == "adm_broadcast":
        ctx.user_data["adm_action"] = "broadcast_msg"
        await q.edit_message_text(
            f"📢 *BROADCAST*\n{DIV}\n💬 Enter the message to send to all approved users:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_panel")]]),
        )
        return

    # ── Admin: Task Manager ──────────────────────────────────────────────────

    if data == "adm_task_manager":
        with get_db() as db:
            tasks = db.execute(
                "SELECT * FROM force_join_tasks ORDER BY created_at DESC"
            ).fetchall()
        if not tasks:
            await q.edit_message_text(
                f"🎯 *TASK MANAGER*\n{DIV}\n📭 _No tasks yet._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [btn("➕ CREATE TASK", "adm_task_create")],
                    [btn("🔙 BACK", "adm_panel")],
                ]),
            )
            return
        kb = []
        lines = []
        for t in tasks:
            flag = "✅" if t["enabled"] else "❌"
            lines.append(f"{flag} `{t['id'][:8]}` — {t['title'][:25]}")
            kb.append([
                btn(f"{'🔛' if not t['enabled'] else '🔕'} {t['id'][:6]}", f"adm_task_toggle:{t['id']}"),
                btn(f"📢 Channels",  f"adm_task_channels:{t['id']}"),
                btn(f"🗑 Delete",    f"adm_task_delete_confirm:{t['id']}"),
            ])
        kb.append([btn("➕ CREATE TASK", "adm_task_create")])
        kb.append([btn("🔙 BACK", "adm_panel")])
        await q.edit_message_text(
            f"🎯 *FORCE JOIN TASKS*\n{DIV}\n" + "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "adm_task_create":
        ctx.user_data["adm_action"] = "task_title"
        ctx.user_data["new_task"] = {}
        await q.edit_message_text(
            "🎯 *Create Task*\n─\nStep 1/3 — Enter the task title:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_task_manager")]]),
        )
        return

    if data.startswith("adm_task_toggle:"):
        task_id = data.split(":", 1)[1]
        with get_db() as db:
            t = db.execute(
                "SELECT enabled FROM force_join_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if t:
                new = 0 if t["enabled"] else 1
                db.execute(
                    "UPDATE force_join_tasks SET enabled=? WHERE id=?", (new, task_id)
                )
        await q.answer(f"Task {'enabled' if new else 'disabled'}.")
        # Re-show task manager
        ctx.user_data.pop("adm_action", None)
        await _show_task_manager(q, ctx)
        return

    if data.startswith("adm_task_delete_confirm:"):
        task_id = data.split(":", 1)[1]
        await q.edit_message_text(
            f"🗑 *DELETE TASK*\n{DIV}\n"
            f"Delete task `{task_id[:8]}`?\n\n"
            f"⚠️ _This removes the task and its channel config._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("🟥 YES, DELETE", f"adm_task_delete:{task_id}"), btn("❌ CANCEL", "adm_task_manager")],
            ]),
        )
        return

    if data.startswith("adm_task_delete:"):
        task_id = data.split(":", 1)[1]
        with get_db() as db:
            db.execute("DELETE FROM force_join_channels WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM force_join_tasks WHERE id=?", (task_id,))
        log_audit(uid, "task_delete", 0, f"task_id={task_id}")
        await q.edit_message_text(
            f"✅ Task `{task_id[:8]}` deleted.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("🔙 TASK MANAGER", "adm_task_manager")]]),
        )
        return

    if data.startswith("adm_task_channels:"):
        task_id = data.split(":", 1)[1]
        channels = get_task_channels(task_id)
        lines = [
            f"  • `{c['id']}` {c['channel_username'] or c['channel_id']}"
            for c in channels
        ]
        kb = []
        for c in channels:
            kb.append([btn(f"🗑 Remove {c['channel_username'] or c['channel_id']}", f"adm_task_rm_channel:{c['id']}")])
        kb.append([btn("➕ ADD CHANNEL", f"adm_task_add_channel:{task_id}")])
        kb.append([btn("🔙 TASK MANAGER", "adm_task_manager")])
        await q.edit_message_text(
            f"📢 *Channels for task* `{task_id[:8]}`\n{DIV}\n"
            + ("\n".join(lines) or "_No channels._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("adm_task_add_channel:"):
        task_id = data.split(":", 1)[1]
        ctx.user_data["adm_action"] = f"add_channel:{task_id}"
        await q.edit_message_text(
            f"📢 *Add Channel*\n─\n"
            f"Task: `{task_id[:8]}`\n\n"
            f"Send the channel username (e.g. `@mychannel`) or channel ID (e.g. `-1001234567890`):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", f"adm_task_channels:{task_id}")]]),
        )
        return

    if data.startswith("adm_task_rm_channel:"):
        chan_id = int(data.split(":", 1)[1])
        with get_db() as db:
            c = db.execute("SELECT task_id FROM force_join_channels WHERE id=?", (chan_id,)).fetchone()
            task_id = c["task_id"] if c else None
            db.execute("DELETE FROM force_join_channels WHERE id=?", (chan_id,))
        await q.answer("Channel removed.")
        if task_id:
            # Simulate re-fetch channels view
            data = f"adm_task_channels:{task_id}"
            # Re-route: rebuild channels view
            channels = get_task_channels(task_id)
            lines = [
                f"  • `{c['id']}` {c['channel_username'] or c['channel_id']}"
                for c in channels
            ]
            kb = []
            for c in channels:
                kb.append([btn(f"🗑 Remove {c['channel_username'] or c['channel_id']}", f"adm_task_rm_channel:{c['id']}")])
            kb.append([btn("➕ ADD CHANNEL", f"adm_task_add_channel:{task_id}")])
            kb.append([btn("🔙 TASK MANAGER", "adm_task_manager")])
            await q.edit_message_text(
                f"📢 *Channels for task* `{task_id[:8]}`\n{DIV}\n"
                + ("\n".join(lines) or "_No channels._"),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        return

    # ── Admin: Mail Manager ──────────────────────────────────────────────────

    if data == "adm_mail_manager":
        stats = get_mail_stats()
        await q.edit_message_text(
            f"📧 *MAIL MANAGER*\n{DIV}\n"
            f"📦 Total: `{stats['total']}`\n"
            f"✅ Available: `{stats['available']}`\n"
            f"📋 Assigned: `{stats['assigned']}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("➕ ADD MAIL",      "adm_mail_add"),
                 btn("📋 LIST MAIL",     "adm_mail_list:0")],
                [btn("📊 STATS",        "adm_mail_stats"),
                 btn("🔙 BACK", "adm_panel")],
            ]),
        )
        return

    if data == "adm_mail_add":
        ctx.user_data["adm_action"] = "mail_add"
        await q.edit_message_text(
            f"📧 *ADD HI2 MAIL*\n{DIV}\n✉️ Enter the mail address to add:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_mail_manager")]]),
        )
        return

    if data.startswith("adm_mail_list:"):
        page = int(data.split(":")[1])
        page_size = 10
        with get_db() as db:
            total = db.execute("SELECT COUNT(*) AS c FROM hi2_mail").fetchone()["c"]
            mails = db.execute(
                "SELECT id, address, status, assigned_to FROM hi2_mail ORDER BY id DESC LIMIT ? OFFSET ?",
                (page_size, page * page_size),
            ).fetchall()
        lines = [
            f"{'✅' if m['status'] == 'available' else '📋'} `{m['id']}` — `{m['address']}`"
            + (f" → `{m['assigned_to']}`" if m["assigned_to"] else "")
            for m in mails
        ]
        nav = []
        if page > 0:
            nav.append(btn("◀️", f"adm_mail_list:{page-1}"))
        if (page + 1) * page_size < total:
            nav.append(btn("▶️", f"adm_mail_list:{page+1}"))
        kb_rows = []
        for m in mails:
            if m["status"] == "available":
                kb_rows.append([btn(f"🗑 Remove #{m['id']}", f"adm_mail_remove:{m['id']}")])
        if nav:
            kb_rows.append(nav)
        kb_rows.append([btn("🔙 MAIL MANAGER", "adm_mail_manager")])
        await q.edit_message_text(
            f"📧 *Mail List* ({total} total)\n{DIV}\n" + ("\n".join(lines) or "_No mail._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return

    if data.startswith("adm_mail_remove:"):
        mail_id = int(data.split(":", 1)[1])
        ok = delete_mail(mail_id)
        await q.answer("✅ Removed." if ok else "⚠️ Cannot remove assigned mail.", show_alert=not ok)
        # Re-show list
        with get_db() as db:
            total = db.execute("SELECT COUNT(*) AS c FROM hi2_mail").fetchone()["c"]
        await q.edit_message_text(
            f"📧 *Mail List*\n{DIV}\n{'✅ Removed.' if ok else '⚠️ That entry is assigned and cannot be removed.'}\n\nTotal: `{total}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("📋 VIEW LIST", "adm_mail_list:0"), btn("🔙", "adm_mail_manager")]
            ]),
        )
        return

    if data == "adm_mail_stats":
        stats = get_mail_stats()
        with get_db() as db:
            recent = db.execute(
                """SELECT h.address, u.full_name, h.assigned_at
                   FROM hi2_mail h LEFT JOIN users u ON h.assigned_to=u.telegram_id
                   WHERE h.status='assigned' ORDER BY h.assigned_at DESC LIMIT 5"""
            ).fetchall()
        lines = [
            f"  • `{r['address']}` → {r['full_name'] or 'N/A'} _{r['assigned_at'][:10] if r['assigned_at'] else ''}_"
            for r in recent
        ]
        await q.edit_message_text(
            f"📊 *Mail Statistics*\n{DIV}\n"
            f"📦 Total: `{stats['total']}`\n"
            f"✅ Available: `{stats['available']}`\n"
            f"📋 Assigned: `{stats['assigned']}`\n\n"
            f"*Recent Assignments*\n" + ("\n".join(lines) or "_None._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("🔙", "adm_mail_manager")]]),
        )
        return

    if data == "adm_task_mail_stats":
        with get_db() as db:
            total_fj  = db.execute("SELECT COUNT(*) AS c FROM force_join_tasks").fetchone()["c"]
            enabled   = db.execute("SELECT COUNT(*) AS c FROM force_join_tasks WHERE enabled=1").fetchone()["c"]
            completions = db.execute("SELECT COUNT(*) AS c FROM task_completions").fetchone()["c"]
        mail_stats = get_mail_stats()
        await q.edit_message_text(
            f"📊 *Task & Mail Stats*\n{DIV}\n"
            f"🎯 Force Join Tasks: `{total_fj}` (✅ {enabled} active)\n"
            f"✔️ Total completions: `{completions}`\n\n"
            f"📧 Mail Pool\n"
            f"  • Total: `{mail_stats['total']}`\n"
            f"  • Available: `{mail_stats['available']}`\n"
            f"  • Assigned: `{mail_stats['assigned']}`",
            parse_mode="Markdown",
            reply_markup=back_to_admin_kb(),
        )
        return

    # ── Admin: Proxy Manager (Enhanced v3) ───────────────────────────────────

    async def _render_proxy_manager(filter_status: str = "all"):
        """
        Shared renderer for the Proxy Manager panel.
        filter_status ∈ {'all', 'live', 'dead', 'untested'}
        """
        stats = proxy_stats()
        proxies = proxy_list_filtered(filter_status if filter_status != "all" else None)

        summary = (
            f"📦 Total: `{stats['total']}`  "
            f"🟢 Live: `{stats['live']}`  "
            f"🔴 Dead: `{stats['dead']}`  "
            f"⚪ Untested: `{stats['untested']}`"
        )

        # Build list lines (cap at 30 for message length)
        lines = []
        shown = proxies[:30]
        for p in shown:
            icon = (
                "🟢" if p["status"] == "live"
                else "🔴" if p["status"] == "dead"
                else "⚪"
            )
            tested = p["last_tested"][:16] if p["last_tested"] else "never"
            lines.append(f"{icon} `#{p['id']}` `{p['proxy_url']}` _{tested}_")
        body = "\n".join(lines) if lines else "_No proxies in this view._"
        if len(proxies) > 30:
            body += f"\n\n_… and {len(proxies) - 30} more (use Export to see all)._"

        # Filter tab row
        filter_row = [
            btn(f"{'●' if filter_status == 'all' else '○'} ALL", "adm_proxy_filter:all"),
            btn(f"{'●' if filter_status == 'live' else '○'} LIVE", "adm_proxy_filter:live"),
            btn(f"{'●' if filter_status == 'dead' else '○'} DEAD", "adm_proxy_filter:dead"),
            btn(f"{'●' if filter_status == 'untested' else '○'} NEW", "adm_proxy_filter:untested"),
        ]

        kb = [
            filter_row,
            [btn("➕ ADD ONE",     "adm_proxy_add"),
             btn("📝 BULK ADD",   "adm_proxy_bulk_add"),
             btn("📤 UPLOAD TXT", "adm_proxy_upload_txt")],
            [btn("🧪 TEST ALL",      "adm_proxy_test_all"),
             btn("🔬 SMART TEST",    "adm_proxy_smart_test_all"),
             btn("📤 EXPORT",        "adm_proxy_export:all")],
            [btn("♻️ RE-TEST DEAD",  "adm_proxy_retest_dead"),
             btn("🩺 REVIVE ALL",    "adm_proxy_revive_all"),
             btn("♻️ AUTO-REVIVE",   "adm_proxy_revive_force")],
            [btn("🗑 CLEAR DEAD",   "adm_proxy_clear_dead_confirm"),
             btn("💥 CLEAR ALL",    "adm_proxy_clear_all_confirm")],
        ]
        # Per-proxy test/delete (respect filter, cap at 20 buttons)
        for p in shown[:20]:
            icon = (
                "🟢" if p["status"] == "live"
                else "🔴" if p["status"] == "dead"
                else "⚪"
            )
            kb.append([
                btn(f"{icon} #{p['id']} Test", f"adm_proxy_test:{p['id']}"),
                btn(f"🗑 #{p['id']}",           f"adm_proxy_del:{p['id']}?f={filter_status}"),
            ])
        kb.append([btn("🔙 ADMIN PANEL", "adm_panel")])
        return (
            f"🌐 *PROXY MANAGER* — `{filter_status.upper()}`\n{DIV}\n"
            f"{summary}\n{SUB}\n{body}",
            InlineKeyboardMarkup(kb),
        )

    if data == "adm_proxy_manager":
        text, markup = await _render_proxy_manager("all")
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data.startswith("adm_proxy_filter:"):
        fstatus = data.split(":", 1)[1]
        text, markup = await _render_proxy_manager(fstatus)
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data == "adm_proxy_add":
        ctx.user_data["adm_action"] = "proxy_add"
        await q.edit_message_text(
            f"🌐 *ADD PROXY*\n{DIV}\n"
            f"Send the proxy in one of these formats:\n\n"
            f"`host:port`\n"
            f"`user:pass@host:port`\n"
            f"`http://host:port`\n"
            f"`socks5://user:pass@host:port`\n\n"
            f"_You can also send multiple proxies (one per line, comma or space separated)._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_proxy_manager")]]),
        )
        return

    if data == "adm_proxy_bulk_add":
        ctx.user_data["adm_action"] = "proxy_add"  # same handler — bulk-aware
        await q.edit_message_text(
            f"📝 *BULK ADD PROXIES*\n{DIV}\n"
            f"Send a list of proxies — one per line, or comma / space / pipe separated.\n\n"
            f"*Supported formats:* http / https / socks5 / socks4\n"
            f"*Examples:*\n"
            f"```\n"
            f"1.2.3.4:8080\n"
            f"user:pass@5.6.7.8:3128\n"
            f"socks5://user:pass@9.10.11.12:1080\n"
            f"```\n"
            f"_Duplicates are automatically skipped. Invalid entries are ignored._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_proxy_manager")]]),
        )
        return

    if data == "adm_proxy_upload_txt":
        ctx.user_data["adm_action"] = "proxy_upload_txt"
        ctx.user_data["awaiting"] = "proxy_txt_file"
        await q.edit_message_text(
            f"📤 *UPLOAD PROXY LIST*\n{DIV}\n"
            f"Send a `.txt` file containing one proxy per line.\n\n"
            f"_Max size: {MAX_UPLOAD_MB} MB  •  Duplicates auto-skipped_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_proxy_manager")]]),
        )
        return

    if data == "adm_proxy_export:all":
        all_proxies = proxy_list()
        if not all_proxies:
            await q.answer("No proxies to export.", show_alert=True)
            return
        content = proxy_export()
        await ctx.bot.send_document(
            uid,
            document=io.BytesIO(content.encode()),
            filename=f"proxies_all_{int(time.time())}.txt",
            caption=f"📤 Exported `{len(all_proxies)}` proxies",
        )
        await q.answer("Exported!", show_alert=False)
        return

    if data.startswith("adm_proxy_export:"):
        fstatus = data.split(":", 1)[1]
        rows = proxy_list_filtered(fstatus if fstatus != "all" else None)
        if not rows:
            await q.answer(f"No {fstatus} proxies to export.", show_alert=True)
            return
        content = "\n".join(r["proxy_url"] for r in rows)
        await ctx.bot.send_document(
            uid,
            document=io.BytesIO(content.encode()),
            filename=f"proxies_{fstatus}_{int(time.time())}.txt",
            caption=f"📤 Exported `{len(rows)}` {fstatus} proxies",
        )
        await q.answer(f"Exported {len(rows)} {fstatus} proxies.", show_alert=False)
        return

    if data == "adm_proxy_clear_all_confirm":
        stats = proxy_stats()
        if stats["total"] == 0:
            await q.answer("No proxies to clear.", show_alert=True)
            return
        await q.edit_message_text(
            f"💥 *CLEAR ALL PROXIES*\n{DIV}\n"
            f"⚠️ This will permanently delete all `{stats['total']}` proxies:\n"
            f"  🟢 Live: `{stats['live']}`\n"
            f"  🔴 Dead: `{stats['dead']}`\n"
            f"  ⚪ Untested: `{stats['untested']}`\n\n"
            f"_This cannot be undone._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("🟥 YES, CLEAR ALL", "adm_proxy_clear_all_yes"),
                 btn("❌ CANCEL", "adm_proxy_manager")],
            ]),
        )
        return

    if data == "adm_proxy_clear_all_yes":
        n = proxy_clear_all()
        log_audit(uid, "proxy_clear_all", 0, f"removed={n}")
        await q.edit_message_text(
            f"✅ Cleared `{n}` proxies.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("🔙 PROXY MANAGER", "adm_proxy_manager")]]),
        )
        return

    if data == "adm_proxy_clear_dead_confirm":
        stats = proxy_stats()
        if stats["dead"] == 0:
            await q.answer("No dead proxies to clear.", show_alert=True)
            return
        await q.edit_message_text(
            f"🗑 *CLEAR DEAD PROXIES*\n{DIV}\n"
            f"This will delete `{stats['dead']}` dead proxies.\n"
            f"_Live and untested proxies are kept._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("🟥 YES, CLEAR DEAD", "adm_proxy_clear_dead_yes"),
                 btn("❌ CANCEL", "adm_proxy_manager")],
            ]),
        )
        return

    if data == "adm_proxy_clear_dead_yes":
        n = proxy_clear_dead()
        log_audit(uid, "proxy_clear_dead", 0, f"removed={n}")
        await q.edit_message_text(
            f"✅ Cleared `{n}` dead proxies.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("🔙 PROXY MANAGER", "adm_proxy_manager")]]),
        )
        return

    if data.startswith("adm_proxy_test:"):
        pid = int(data.split(":", 1)[1])
        await q.answer("Testing…")
        with get_db() as db:
            row_p = db.execute("SELECT proxy_url FROM proxies WHERE id=?", (pid,)).fetchone()
        if not row_p:
            await q.answer("Proxy not found.", show_alert=True)
            return
        loop_ref = asyncio.get_running_loop()
        result = await loop_ref.run_in_executor(None, _test_one_proxy, pid, row_p["proxy_url"])
        icon = "🟢 Live" if result == "live" else "🔴 Dead"
        await q.edit_message_text(
            f"🧪 *PROXY TEST RESULT*\n{DIV}\n"
            f"`{row_p['proxy_url']}`\n\n"
            f"Result: {icon}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("🔙 PROXY MANAGER", "adm_proxy_manager")]]),
        )
        return

    if data == "adm_proxy_test_all":
        proxies_all = proxy_list()
        if not proxies_all:
            await q.answer("No proxies to test.", show_alert=True)
            return
        await q.edit_message_text(
            f"🧪 Testing `{len(proxies_all)}` proxies in parallel — this may take a moment…",
            parse_mode="Markdown",
        )
        result = await _test_proxies_concurrent(proxies_all, concurrency=10)
        await q.edit_message_text(
            f"🧪 *TEST COMPLETE*\n{DIV}\n"
            f"🟢 Live: `{result['live']}`  |  🔴 Dead: `{result['dead']}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("🔙 PROXY MANAGER", "adm_proxy_manager")]]),
        )
        return

    if data == "adm_proxy_smart_test_all":
        """Full-flow test: captcha + API reachability for every proxy."""
        proxies_all = proxy_list()
        if not proxies_all:
            await q.answer("No proxies to test.", show_alert=True)
            return
        est_secs = max(len(proxies_all) // 8 * 3, 5)
        await q.edit_message_text(
            f"🔬 *Smart Test* — full captcha+API flow for `{len(proxies_all)}` proxies...\n"
            f"⚡ Running 8 concurrent — est. ~{est_secs}s",
            parse_mode="Markdown",
        )
        loop_ref = asyncio.get_running_loop()
        smart_sem = asyncio.Semaphore(8)

        async def _do_smart_test(p):
            async with smart_sem:
                r = await loop_ref.run_in_executor(
                    None, _test_one_proxy_full_flow, p["id"], p["proxy_url"]
                )
                return (p["id"], p["proxy_url"], r)

        results = list(await asyncio.gather(*[_do_smart_test(p) for p in proxies_all]))

        live_c = sum(1 for _, _, r in results if r["overall"] == "live")
        partial_c = sum(1 for _, _, r in results if r["overall"] == "partial")
        dead_c = sum(1 for _, _, r in results if r["overall"] == "dead")
        captcha_ok = sum(1 for _, _, r in results if r["captcha"] == "ok")
        api_ok = sum(1 for _, _, r in results if r["api"] == "ok")

        detail_lines = []
        for pid, purl, r in results[:15]:
            icons = (
                f"{'🟢' if r['connectivity']=='live' else '🔴'}"
                f"{'✅' if r['captcha']=='ok' else '❌'}"
                f"{'✅' if r['api']=='ok' else '❌'}"
            )
            detail_lines.append(f"{icons} `#{pid}` {r['overall']}")

        await q.edit_message_text(
            f"🔬 *SMART TEST COMPLETE*\n{DIV}\n"
            f"🟢 Live (full): `{live_c}`\n"
            f"🟡 Partial (connects, captcha fails): `{partial_c}`\n"
            f"🔴 Dead: `{dead_c}`\n\n"
            f"📊 *BREAKDOWN*\n"
            f"  • Captcha solved: `{captcha_ok}/{len(results)}`\n"
            f"  • hi2.in reachable: `{api_ok}/{len(results)}`\n\n"
            f"_Legend: [connect] [captcha] [api]_\n"
            + ("\n".join(detail_lines) if detail_lines else ""),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("🩺 REVIVE PARTIAL", "adm_proxy_revive_all")],
                [btn("🔙 PROXY MANAGER", "adm_proxy_manager")],
            ]),
        )
        return

    if data == "adm_proxy_revive_all":
        """Revive ALL dead+untested proxies back to 'untested' (in-cycle)."""
        with _db_lock:
            with get_db() as db:
                n = db.execute(
                    "SELECT COUNT(*) AS c FROM proxies WHERE status='dead'"
                ).fetchone()["c"]
                db.execute("UPDATE proxies SET status='untested' WHERE status='dead'")
        _rebuild_proxy_cycle()
        log_audit(uid, "proxy_revive_all", 0, f"revived={n}")
        await q.edit_message_text(
            f"🩺 *Revive All*\n{DIV}\n"
            f"✅ Revived `{n}` dead proxies → status `untested`\n"
            f"_They're now back in the cycle and will be re-tried on next check._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("🔙 PROXY MANAGER", "adm_proxy_manager")]]),
        )
        return

    if data == "adm_proxy_revive_force":
        """Force-run the auto-revive (cooldown-expired) logic immediately."""
        n = _revive_old_dead_proxies(force=True)
        stats = proxy_stats()
        await q.edit_message_text(
            f"♻️ *Auto-Revive (forced)*\n{DIV}\n"
            f"Revived by cooldown: `{n}`\n\n"
            f"Current state:\n"
            f"  🟢 Live: `{stats['live']}`\n"
            f"  🔴 Dead: `{stats['dead']}`\n"
            f"  ⚪ Untested: `{stats['untested']}`\n"
            f"  📦 Total: `{stats['total']}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("🔙 PROXY MANAGER", "adm_proxy_manager")]]),
        )
        return

    if data == "adm_proxy_retest_dead":
        dead_rows = proxy_list_filtered("dead")
        if not dead_rows:
            await q.answer("No dead proxies to re-test.", show_alert=True)
            return
        await q.edit_message_text(
            f"♻️ Re-testing `{len(dead_rows)}` dead proxies in parallel…",
            parse_mode="Markdown",
        )
        # Reset to untested first so they get a fair re-test
        with _db_lock:
            with get_db() as db:
                db.execute("UPDATE proxies SET status='untested' WHERE status='dead'")
        result = await _test_proxies_concurrent(dead_rows, concurrency=10)
        await q.edit_message_text(
            f"♻️ *Re-test Complete*\n{DIV}\n"
            f"Revived 🟢 Live: `{result['live']}`  |  Still 🔴 Dead: `{result['dead']}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("🔙 PROXY MANAGER", "adm_proxy_manager")]]),
        )
        return

    if data.startswith("adm_proxy_del:"):
        # Format: adm_proxy_del:<id>  or  adm_proxy_del:<id>?f=<filter>
        raw_arg = data.split(":", 1)[1]
        filter_status = "all"
        if "?" in raw_arg:
            pid_str, qs = raw_arg.split("?", 1)
            pid = int(pid_str)
            for kv in qs.split("&"):
                if kv.startswith("f="):
                    filter_status = kv[2:]
        else:
            pid = int(raw_arg)
        ok = proxy_delete(pid)
        await q.answer("✅ Deleted." if ok else "⚠️ Not found.", show_alert=not ok)
        text, markup = await _render_proxy_manager(filter_status)
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    # ── Admin: Backup ────────────────────────────────────────────────────────

    # ── Admin: Blacklist (v94) ───────────────────────────────────────────────

    if data == "adm_blacklist":
        rows = blacklist_list(30)
        lines = [
            f"`{r['pattern']}` ({r['kind']}) — {r['reason'] or '-'}"
            for r in rows
        ] or ["_Empty._"]
        await q.edit_message_text(
            f"🛑 *BLACKLIST*\n{DIV}\n"
            + "\n".join(lines)
            + "\n\n_Add: email / domain / prefix pattern_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("➕ ADD", "adm_bl_add"), btn("🗑 REMOVE", "adm_bl_remove")],
                [btn("🔙 ADMIN PANEL", "adm_panel")],
            ]),
        )
        return

    if data == "adm_bl_add":
        ctx.user_data["adm_action"] = "bl_add"
        await q.edit_message_text(
            f"🛑 *ADD BLACKLIST*\n{DIV}\n"
            f"Send pattern in one of these forms:\n"
            f"`email:foo@hi2.in`\n"
            f"`domain:hi2.in`\n"
            f"`prefix:test`\n"
            f"or just an email / domain string.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_blacklist")]]),
        )
        return

    if data == "adm_bl_remove":
        ctx.user_data["adm_action"] = "bl_remove"
        await q.edit_message_text(
            f"🛑 *REMOVE BLACKLIST*\n{DIV}\n"
            f"Send the exact pattern to remove:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("❌ CANCEL", "adm_blacklist")]]),
        )
        return

    if data == "adm_backup":

        await q.edit_message_text(
            f"💾 *DATABASE BACKUP*\n{DIV}\n⏳ Creating backup — this may take a moment...",
            parse_mode="Markdown",
        )
        result = await do_database_backup(ctx.bot)
        await q.edit_message_text(
            f"💾 *DATABASE BACKUP*\n{DIV}\n{result}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("🗂 BACKUP HISTORY", "adm_backup_list"), btn("🔙 BACK", "adm_panel")]
            ]),
        )
        return

    if data == "adm_backup_list":
        with get_db() as db:
            backups = db.execute(
                "SELECT * FROM db_backups ORDER BY id DESC LIMIT 15"
            ).fetchall()
        lines = [
            f"{'✅' if b['result'] == 'ok' else '❌'} `{b['created_at'][:16]}` — {b['filename']} ({b['size_bytes']:,}B)"
            for b in backups
        ]
        await q.edit_message_text(
            f"🗂 *BACKUP HISTORY*\n{DIV}\n" + ("\n".join(lines) or "_No backups._"),
            parse_mode="Markdown",
            reply_markup=back_to_admin_kb(),
        )
        return

    # Unhandled
    log.warning("Unhandled callback: %s from %s", data, uid)


async def _show_task_manager(q, ctx):
    """Helper to re-render task manager."""
    with get_db() as db:
        tasks = db.execute(
            "SELECT * FROM force_join_tasks ORDER BY created_at DESC"
        ).fetchall()
    if not tasks:
        await q.edit_message_text(
            f"🎯 *TASK MANAGER*\n{DIV}\n📭 _No tasks yet._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("➕ CREATE TASK", "adm_task_create")],
                [btn("🔙 BACK", "adm_panel")],
            ]),
        )
        return
    kb = []
    lines = []
    for t in tasks:
        flag = "✅" if t["enabled"] else "❌"
        lines.append(f"{flag} `{t['id'][:8]}` — {t['title'][:25]}")
        kb.append([
            btn(f"{'🔛' if not t['enabled'] else '🔕'} {t['id'][:6]}", f"adm_task_toggle:{t['id']}"),
            btn("📢 CHANNELS",  f"adm_task_channels:{t['id']}"),
            btn("🗑 DELETE",    f"adm_task_delete_confirm:{t['id']}"),
        ])
    kb.append([btn("➕ CREATE TASK", "adm_task_create")])
    kb.append([btn("🔙 BACK", "adm_panel")])
    await q.edit_message_text(
        f"🎯 *FORCE JOIN TASKS*\n{DIV}\n" + "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  DOCUMENT HANDLER (TXT upload)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_user(user.id)
    if not row or row["status"] != "APPROVED":
        return

    awaiting = ctx.user_data.get("awaiting")

    # ── Proxy TXT upload branch (owner-only) ─────────────────────────────────
    if awaiting == "proxy_txt_file":
        if user.id != OWNER_ID:
            return
        doc = update.message.document
        if not doc or not doc.file_name.lower().endswith(".txt"):
            await update.message.reply_text("❌ Only .txt files are accepted.")
            return
        max_bytes = MAX_UPLOAD_MB * 1024 * 1024
        if doc.file_size > max_bytes:
            await update.message.reply_text(f"❌ File too large. Max {MAX_UPLOAD_MB} MB.")
            return
        ctx.user_data.pop("awaiting", None)
        wait_msg = await update.message.reply_text("📤 Reading proxy file...")
        try:
            tg_file = await doc.get_file()
            file_bytes = await tg_file.download_as_bytearray()
            raw = file_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            await wait_msg.edit_text(f"❌ Failed to read file: {e}")
            return
        # Store raw and trigger bulk-process via admin text handler
        ctx.user_data["proxy_upload_raw"] = raw
        ctx.user_data["adm_action"] = "proxy_upload_txt_process"
        # Directly invoke the admin text handler with a synthetic text payload
        # — the handler looks at adm_action and pops proxy_upload_raw itself.
        # We can't pass no text, so we inline the processing here:
        result = proxy_add_bulk(raw)
        log_audit(
            user.id, "proxy_add_bulk_txt", 0,
            f"added={result['added']} duplicate={result['duplicate']}"
        )
        stats = proxy_stats()
        ctx.user_data.pop("adm_action", None)
        ctx.user_data.pop("proxy_upload_raw", None)
        await wait_msg.edit_text(
            f"📤 *TXT IMPORT COMPLETE*\n{DIV}\n"
            f"📄 File: `{doc.file_name}`\n"
            f"✅ Added: `{result['added']}`\n"
            f"🔁 Duplicates skipped: `{result['duplicate']}`\n"
            f"📦 Total in pool: `{stats['total']}`  "
            f"(🟢 `{stats['live']}` / 🔴 `{stats['dead']}` / ⚪ `{stats['untested']}`)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("🧪 TEST ALL NOW", "adm_proxy_test_all")],
                [btn("🌐 PROXY MANAGER", "adm_proxy_manager")],
            ]),
        )
        return

    # ── Email TXT upload branch (original) ───────────────────────────────────
    if awaiting != "txt_file":
        return

    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Only .txt files are accepted.")
        return

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    if doc.file_size > max_bytes:
        await update.message.reply_text(f"❌ File too large. Max {MAX_UPLOAD_MB} MB.")
        return

    ctx.user_data.pop("awaiting", None)

    wait_msg = await update.message.reply_text("📂 Reading file...")

    try:
        tg_file = await doc.get_file()
        file_bytes = await tg_file.download_as_bytearray()
        raw = file_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        await wait_msg.edit_text(f"❌ Failed to read file: {e}")
        return

    emails = _parse_emails(raw)
    if not emails:
        await wait_msg.edit_text("❌ No valid HI2 emails found in file.")
        return

    total = len(emails)
    credits = row["credits"]

    # Create task record
    task_id = str(uuid.uuid4())
    with get_db() as db:
        db.execute(
            "INSERT INTO tasks(id, user_id, filename, total, status) VALUES(?,?,?,?,'PENDING')",
            (task_id, user.id, doc.file_name, total),
        )

    # Store emails in user_data for confirmation
    ctx.user_data[f"pending_emails_{task_id}"] = emails

    await wait_msg.edit_text(
        f"{HEADER}\n{DIV}\n"
        f"📂 *FILE PREVIEW*\n{SUB}\n"
        f"📄 File: `{doc.file_name}`\n"
        f"📧 Emails found: `{total}`\n"
        f"💰 Credits needed: `{total}`\n"
        f"💳 Your balance: `{credits}`\n{SUB}\n"
        + ("✅ _Sufficient credits — ready to launch!_ 🚀"
           if credits >= total
           else f"❌ _Insufficient credits. Need {total - credits} more._"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [btn("🚀 START CHECKING", f"confirm_txt:{task_id}")],
            [btn("❌ CANCEL", "refresh_home")],
        ]) if credits >= total else InlineKeyboardMarkup([[btn("🏠 HOME", "refresh_home")]]),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN TEXT HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()
    action = ctx.user_data.get("adm_action", "")

    async def reply(msg, kb=None):
        await update.message.reply_text(msg, parse_mode="Markdown",
                                        reply_markup=kb or back_to_admin_kb())

    # ── Rewards settings: set:<key> flow ─────────────────────────────────────
    if action.startswith("set:"):
        key = action[4:]
        if key in _INT_SETTING_KEYS:
            try:
                val = str(max(0, int(text)))
            except ValueError:
                await reply("❌ Number bhejo (0+).")
                return
        else:
            val = text
        set_setting(key, val)
        log_audit(uid, "set_setting", 0, f"{key}={val}")
        ctx.user_data.pop("adm_action", None)
        await reply(f"✅ `{key}` = `{val}` saved!")
        return

    # ── FJ channel add flow ──────────────────────────────────────────────────
    if action == "fj_add":
        ctx.user_data.pop("adm_action", None)
        raw = text.split()[0] if text.split() else ""
        if not raw:
            await reply("❌ Channel id bhejo (`@name` ya `-100...`).")
            return
        title = ""
        try:
            chat = await ctx.bot.get_chat(raw)
            title = chat.title or ""
            raw = f"@{chat.username}" if chat.username else str(chat.id)
        except Exception:
            pass
        ok = fj_gate_add(raw, title)
        log_audit(uid, "fj_channel_add", 0, f"{raw} ({title})" if title else raw)
        await reply(
            ("✅ Channel added: " if ok else "❌ Add failed: ") + f"`{raw}`"
            + (f" — *{title}*" if title else "")
            + "\n⚠️ _Bot ko channel me ADMIN banana zaroori hai!_")
        return

    # ── Custom start message flow ────────────────────────────────────────────
    if action == "startmsg":
        ctx.user_data.pop("adm_action", None)
        val = "" if text.lower() in ("off", "none", "clear") else text
        set_setting("custom_start_text", val)
        log_audit(uid, "set_setting", 0, f"custom_start_text={'OFF' if not val else 'custom'}")
        await reply("✅ Start message update ho gaya!" if val
                    else "✅ Custom start message OFF — default welcome wapas.")
        return

    # ── Give credits flow ────────────────────────────────────────────────────
    if action == "give_uid":
        try:
            target = int(text)
        except ValueError:
            await reply("❌ Invalid ID. Enter a number.")
            return
        if not get_user(target):
            await reply(f"❌ User `{target}` not found.")
            ctx.user_data.pop("adm_action", None)
            return
        ctx.user_data["adm_action"] = "give_amount"
        ctx.user_data["adm_target"] = target
        await reply(f"💰 Give credits to `{target}`\nEnter amount:")
        return

    if action == "give_amount":
        try:
            amount = int(text)
            assert amount > 0
        except (ValueError, AssertionError):
            await reply("❌ Invalid amount.")
            return
        target = ctx.user_data.pop("adm_target", None)
        ctx.user_data.pop("adm_action", None)
        if not target:
            await reply("❌ Session lost. Start again.")
            return
        add_credits(target, amount, "admin_grant", uid)
        log_audit(uid, "give_credits", target, f"amount={amount}")
        try:
            await ctx.bot.send_message(
                target,
                f"{HEADER}\n{DIV}\n"
                f"💰 *+{amount} CREDITS ADDED!*\n{SUB}\n"
                f"👤 The owner added `{amount}` credit(s) to your account.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        await reply(f"✅ Gave `{amount}` credits to `{target}`.")
        return

    # ── Remove credits flow ──────────────────────────────────────────────────
    if action == "remove_uid":
        try:
            target = int(text)
        except ValueError:
            await reply("❌ Invalid ID.")
            return
        if not get_user(target):
            await reply(f"❌ User `{target}` not found.")
            ctx.user_data.pop("adm_action", None)
            return
        ctx.user_data["adm_action"] = "remove_amount"
        ctx.user_data["adm_target"] = target
        await reply(f"➖ Remove credits from `{target}`\nEnter amount:")
        return

    if action == "remove_amount":
        try:
            amount = int(text)
            assert amount > 0
        except (ValueError, AssertionError):
            await reply("❌ Invalid amount.")
            return
        target = ctx.user_data.pop("adm_target", None)
        ctx.user_data.pop("adm_action", None)
        ok = remove_credits(target, amount, uid)
        log_audit(uid, "remove_credits", target, f"amount={amount}")
        await reply(
            f"✅ Removed credits from `{target}`." if ok
            else f"⚠️ `{target}` has fewer credits than requested — all available credits removed."
        )
        return

    # ── Ban/Unban ────────────────────────────────────────────────────────────
    if action == "ban_uid":
        ctx.user_data.pop("adm_action", None)
        try:
            target = int(text)
        except ValueError:
            await reply("❌ Invalid ID.")
            return
        row = get_user(target)
        if not row:
            await reply(f"❌ User `{target}` not found.")
            return
        new_status = "APPROVED" if row["status"] == "BANNED" else "BANNED"
        with get_db() as db:
            db.execute("UPDATE users SET status=? WHERE telegram_id=?", (new_status, target))
        log_audit(uid, new_status.lower(), target)
        await reply(f"{'🚫 Banned' if new_status == 'BANNED' else '✅ Unbanned'} user `{target}`.")
        return

    # ── Search user ──────────────────────────────────────────────────────────
    if action == "search_uid":
        ctx.user_data.pop("adm_action", None)
        try:
            target = int(text)
        except ValueError:
            await reply("❌ Invalid ID.")
            return
        row = get_user(target)
        if not row:
            await reply(f"❌ User `{target}` not found.")
            return
        with get_db() as db:
            txns = db.execute(
                "SELECT amount, reason, created_at FROM credit_transactions WHERE user_id=? ORDER BY id DESC LIMIT 5",
                (target,),
            ).fetchall()
            t_count = db.execute("SELECT COUNT(*) AS c FROM tasks WHERE user_id=?", (target,)).fetchone()["c"]
        prem_info = get_premium_info(target)
        prem_str = (
            f"💎 {prem_info['plan']} exp `{prem_info['expires_at'][:10]}`"
            if prem_info and is_premium(target) else "⚪ No premium"
        )
        txn_lines = "\n".join(
            f"  {'🟢' if t['amount'] > 0 else '🔴'} {t['amount']:+d} — {t['reason']}"
            for t in txns
        ) or "  _None._"
        kb = InlineKeyboardMarkup([
            [btn(f"💎 Grant Premium", f"adm_premium_grant"), btn(f"🔴 BAN / UNBAN", "adm_ban")],
            [btn("🔙 ADMIN PANEL", "adm_panel")],
        ])
        ctx.user_data["adm_target"] = target
        await update.message.reply_text(
            f"🔍 *USER PROFILE — {row['full_name']}*\n{DIV}\n"
            f"🆔 `{target}` | @{row['username'] or 'N/A'}\n"
            f"📊 Status: `{row['status']}`\n"
            f"💰 Credits: `{row['credits']}`\n"
            f"🔍 Checks: `{row['total_checks']}`\n"
            f"📋 Tasks: `{t_count}`\n"
            f"🎯 {prem_str}\n"
            f"📅 Joined: `{row['registered_at'][:10]}`\n\n"
            f"*Recent Transactions*\n{txn_lines}",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    # ── Broadcast ────────────────────────────────────────────────────────────
    if action == "broadcast_msg":
        ctx.user_data.pop("adm_action", None)
        with get_db() as db:
            recipients = db.execute(
                "SELECT telegram_id FROM users WHERE status='APPROVED'"
            ).fetchall()
        sent = failed = 0
        status_msg = await update.message.reply_text(f"📢 Sending to {len(recipients)} users...")
        for r in recipients:
            try:
                await ctx.bot.send_message(r["telegram_id"], text, parse_mode="Markdown")
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)  # rate-limit guard
        await status_msg.edit_text(
            f"📢 *BROADCAST COMPLETE*\n{DIV}\n✅ Sent: `{sent}`  •  ❌ Failed: `{failed}`",
            parse_mode="Markdown",
        )
        return

    # ── Premium grant flow ───────────────────────────────────────────────────
    if action == "premium_uid":
        try:
            target = int(text)
        except ValueError:
            await reply("❌ Invalid ID.")
            return
        if not get_user(target):
            await reply(f"❌ User `{target}` not found.")
            ctx.user_data.pop("adm_action", None)
            return
        ctx.user_data["adm_target"] = target
        ctx.user_data["adm_action"] = "premium_plan"
        plan_kb = InlineKeyboardMarkup([
            [btn(f"🟣 {plan} — {days} DAYS", f"adm_premium_plan:{target}:{plan}")]
            for plan, days in PLAN_DAYS.items()
        ] + [[btn("❌ CANCEL", "adm_premium")]])
        await update.message.reply_text(
            f"💎 *Grant Premium* to `{target}`\nSelect a plan:",
            parse_mode="Markdown",
            reply_markup=plan_kb,
        )
        ctx.user_data.pop("adm_action", None)
        return

    # ── Bonus amount ─────────────────────────────────────────────────────────
    if action == "bonus_amount":
        ctx.user_data.pop("adm_action", None)
        try:
            amount = int(text)
            assert amount > 0
        except (ValueError, AssertionError):
            await reply("❌ Enter a positive integer.")
            return
        set_setting("daily_bonus_amount", str(amount))
        await reply(f"✅ Daily bonus set to `{amount}` credit(s).")
        return

    # ── Referral reward ──────────────────────────────────────────────────────
    if action == "referral_reward":
        ctx.user_data.pop("adm_action", None)
        try:
            amount = int(text)
            assert amount >= 0
        except (ValueError, AssertionError):
            await reply("❌ Enter a non-negative integer.")
            return
        set_setting("referral_reward", str(amount))
        await reply(f"✅ Referral reward set to `{amount}` credit(s).")
        return

    # ── Redeem code creation flow ─────────────────────────────────────────────
    if action == "code_create":
        code = text.strip().upper()
        if code == "AUTO":
            code = str(uuid.uuid4())[:8].upper()
        ctx.user_data["new_code"]["code"] = code
        ctx.user_data["adm_action"] = "code_type"
        await update.message.reply_text(
            f"🎟️ Code: `{code}`\n{SUB}\n👇 Select reward type:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [btn("🟡 CREDITS", f"_codetype:credits"), btn("🟣 PREMIUM DAYS", f"_codetype:premium_days")]
            ]),
        )
        ctx.user_data.pop("adm_action", None)
        return

    if action == "code_amount":
        try:
            amount = int(text)
            assert amount > 0
        except (ValueError, AssertionError):
            await reply("❌ Enter a positive integer.")
            return
        ctx.user_data["new_code"]["amount"] = amount
        ctx.user_data["adm_action"] = "code_uses"
        await update.message.reply_text(
            f"🔁 *MAX USES*\n{DIV}\n🔢 Enter a number, or `0` for unlimited:",
            parse_mode="Markdown",
        )
        return

    if action == "code_uses":
        try:
            uses = int(text)
            assert uses >= 0
        except (ValueError, AssertionError):
            await reply("❌ Enter 0 or a positive integer.")
            return
        ctx.user_data["new_code"]["max_uses"] = uses if uses > 0 else 99999
        ctx.user_data["adm_action"] = "code_expiry"
        await update.message.reply_text(
            f"📅 *EXPIRY DATE*\n{DIV}\n🗓 Enter `YYYY-MM-DD`, or `NO` for none:",
            parse_mode="Markdown",
        )
        return

    if action == "code_expiry":
        ctx.user_data.pop("adm_action", None)
        expires = None
        if text.upper() != "NO":
            try:
                expires = datetime.strptime(text.strip(), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                ).isoformat()
            except ValueError:
                await reply("❌ Invalid date. Use YYYY-MM-DD or NO.")
                return
        nc = ctx.user_data.pop("new_code", {})
        ok = create_redeem_code(
            nc.get("code", ""),
            nc.get("reward_type", "credits"),
            nc.get("amount", 1),
            nc.get("max_uses", 1),
            expires,
        )
        if ok:
            log_audit(uid, "create_redeem_code", 0, f"code={nc.get('code')}")
            await reply(
                f"✅ Code `{nc.get('code')}` created.\n"
                f"Type: `{nc.get('reward_type')}` | Amount: `{nc.get('amount')}` | Uses: `{nc.get('max_uses')}`"
            )
        else:
            await reply("❌ Code already exists.")
        return

    # ── Force Join Task creation flow ─────────────────────────────────────────
    if action == "task_title":
        ctx.user_data["new_task"]["title"] = text
        ctx.user_data["adm_action"] = "task_desc"
        await update.message.reply_text(
            "🎯 *Create Task*\nStep 2/3 — Enter the task description:",
            parse_mode="Markdown",
        )
        return

    if action == "task_desc":
        ctx.user_data["new_task"]["description"] = text
        ctx.user_data["adm_action"] = "task_channel_first"
        await update.message.reply_text(
            "🎯 *Create Task*\nStep 3/3 — Enter required channel (@username or -100ID):\n"
            "_Send DONE to skip channels._",
            parse_mode="Markdown",
        )
        return

    if action == "task_channel_first":
        task_channels = []
        nt = ctx.user_data.get("new_task", {})
        if text.upper() == "DONE":
            # Create task with no channels
            task_id = str(uuid.uuid4())
            with get_db() as db:
                db.execute(
                    "INSERT INTO force_join_tasks(id, title, description) VALUES(?,?,?)",
                    (task_id, nt.get("title", ""), nt.get("description", "")),
                )
            ctx.user_data.pop("new_task", None)
            ctx.user_data.pop("adm_action", None)
            log_audit(uid, "create_task", 0, f"task_id={task_id}")
            await update.message.reply_text(
                f"✅ Task `{task_id[:8]}` created (no channels).",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[btn("🟠 TASK MANAGER", "adm_task_manager")]]),
            )
        else:
            chan_id = text.strip()
            display = chan_id
            task_id = str(uuid.uuid4())
            with get_db() as db:
                db.execute(
                    "INSERT INTO force_join_tasks(id, title, description) VALUES(?,?,?)",
                    (task_id, nt.get("title", ""), nt.get("description", "")),
                )
                db.execute(
                    "INSERT INTO force_join_channels(task_id, channel_id, channel_username) VALUES(?,?,?)",
                    (task_id, chan_id, display),
                )
            ctx.user_data.pop("new_task", None)
            ctx.user_data.pop("adm_action", None)
            log_audit(uid, "create_task", 0, f"task_id={task_id}")
            await update.message.reply_text(
                f"✅ Task `{task_id[:8]}` created with channel `{display}`.\n"
                f"Add more channels from Task Manager → Channels.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [btn("🟠 TASK MANAGER", "adm_task_manager")]
                ]),
            )
        return

    # ── Add channel to existing task ──────────────────────────────────────────
    if action.startswith("add_channel:"):
        task_id = action.split(":", 1)[1]
        ctx.user_data.pop("adm_action", None)
        chan_id = text.strip()
        with get_db() as db:
            db.execute(
                "INSERT INTO force_join_channels(task_id, channel_id, channel_username) VALUES(?,?,?)",
                (task_id, chan_id, chan_id),
            )
        log_audit(uid, "task_add_channel", 0, f"task_id={task_id} channel={chan_id}")
        await update.message.reply_text(
            f"✅ Channel `{chan_id}` added to task `{task_id[:8]}`.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn("📢 View Channels", f"adm_task_channels:{task_id}")]]),
        )
        return

    # ── Mail add ──────────────────────────────────────────────────────────────
    if action == "mail_add":
        ctx.user_data.pop("adm_action", None)
        address = text.strip()
        if "@" not in address:
            await reply("❌ Invalid email address.")
            return
        ok = add_mail(address)
        if ok:
            log_audit(uid, "mail_add", 0, f"address={address}")
            await reply(f"✅ Mail `{address}` added to pool.")
        else:
            await reply(f"⚠️ `{address}` already exists in the pool.")
        return

    # ── Proxy add (single OR bulk) ───────────────────────────────────────────
    if action == "proxy_add":
        ctx.user_data.pop("adm_action", None)
        raw = text.strip()
        if not raw:
            await reply("❌ Empty input. Send a proxy URL or list.")
            return

        # Detect multi-proxy input: newline / comma / semicolon / pipe / whitespace
        # containing more than one token
        parsed = _parse_proxy_lines(raw)
        if not parsed:
            await reply(
                "❌ No valid proxies found. Use format `host:port` or `user:pass@host:port`.",
                InlineKeyboardMarkup([[btn("🌐 PROXY MANAGER", "adm_proxy_manager")]]),
            )
            return

        if len(parsed) == 1:
            # Single proxy — original flow (preserves exact audit + message style)
            ok = proxy_add(raw)
            if ok:
                _rebuild_proxy_cycle()
                log_audit(uid, "proxy_add", 0, f"proxy={raw}")
                await reply(
                    f"✅ Proxy added:\n`{_build_proxy_url(raw)}`\n\nTest it from Proxy Manager.",
                    InlineKeyboardMarkup([[btn("🌐 PROXY MANAGER", "adm_proxy_manager")]]),
                )
            else:
                await reply(
                    f"⚠️ Proxy already exists:\n`{parsed[0]}`",
                    InlineKeyboardMarkup([[btn("🌐 PROXY MANAGER", "adm_proxy_manager")]]),
                )
            return

        # Bulk add path
        result = proxy_add_bulk(raw)
        log_audit(
            uid, "proxy_add_bulk", 0,
            f"added={result['added']} duplicate={result['duplicate']}"
        )
        stats = proxy_stats()
        msg_lines = [
            f"📝 *Bulk Add Result*\n{DIV}",
            f"✅ Added: `{result['added']}`",
            f"🔁 Duplicates skipped: `{result['duplicate']}`",
            f"📦 Total in pool: `{stats['total']}`  (🟢 `{stats['live']}` / 🔴 `{stats['dead']}` / ⚪ `{stats['untested']}`)",
        ]
        if result["added_urls"]:
            preview = "\n".join(f"  • `{u}`" for u in result["added_urls"][:10])
            if len(result["added_urls"]) > 10:
                preview += f"\n  _… and {len(result['added_urls']) - 10} more._"
            msg_lines.append(f"\n*Added proxies:*")
            msg_lines.append(preview)
        await reply(
            "\n".join(msg_lines),
            InlineKeyboardMarkup([
                [btn("🧪 TEST ALL NOW", "adm_proxy_test_all")],
                [btn("🌐 PROXY MANAGER", "adm_proxy_manager")],
            ]),
        )
        return

    # ── Proxy TXT upload completion (called after handle_document stores raw) ─
    if action == "proxy_upload_txt_process":
        raw = ctx.user_data.pop("proxy_upload_raw", "")
        ctx.user_data.pop("adm_action", None)
        if not raw:
            await reply("❌ No file content captured. Please re-upload.")
            return
        result = proxy_add_bulk(raw)
        log_audit(
            uid, "proxy_add_bulk_txt", 0,
            f"added={result['added']} duplicate={result['duplicate']}"
        )
        stats = proxy_stats()
        await reply(
            f"📤 *TXT IMPORT COMPLETE*\n{DIV}\n"
            f"✅ Added: `{result['added']}`\n"
            f"🔁 Duplicates skipped: `{result['duplicate']}`\n"
            f"📦 Total in pool: `{stats['total']}`  "
            f"(🟢 `{stats['live']}` / 🔴 `{stats['dead']}` / ⚪ `{stats['untested']}`)",
            InlineKeyboardMarkup([
                [btn("🧪 TEST ALL NOW", "adm_proxy_test_all")],
                [btn("🌐 PROXY MANAGER", "adm_proxy_manager")],
            ]),
        )
        return

    # ── Blacklist add / remove (v94) ─────────────────────────────────────────
    if action == "bl_add":
        ctx.user_data.pop("adm_action", None)
        raw = text.strip()
        kind = "email"
        pattern = raw
        reason = ""
        if ":" in raw:
            parts = raw.split(":", 1)
            k = parts[0].lower().strip()
            if k in ("email", "domain", "prefix"):
                kind = k
                pattern = parts[1].strip()
        ok = blacklist_add(pattern, kind=kind, reason=reason, created_by=uid)
        log_audit(uid, "blacklist_add", 0, f"{kind}:{pattern}")
        await reply(
            f"{'✅ Added' if ok else '⚠️ Already exists / failed'}: `{kind}:{pattern}`",
            InlineKeyboardMarkup([[btn("🛑 BLACKLIST", "adm_blacklist")]]),
        )
        return

    if action == "bl_remove":
        ctx.user_data.pop("adm_action", None)
        ok = blacklist_remove(text.strip())
        log_audit(uid, "blacklist_remove", 0, text.strip())
        await reply(
            f"{'✅ Removed' if ok else '⚠️ Not found'}: `{text.strip()}`",
            InlineKeyboardMarkup([[btn("🛑 BLACKLIST", "adm_blacklist")]]),
        )
        return

    # Unhandled admin action — clear and warn
    if action:
        ctx.user_data.pop("adm_action", None)
        await reply(f"⚠️ Unrecognized action `{action}`. Cleared.")


# ══════════════════════════════════════════════════════════════════════════════
#  CALLBACK — REDEEM CODE TYPE SELECTION (inline interim button)
# ══════════════════════════════════════════════════════════════════════════════

async def callback_code_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    if not data.startswith("_codetype:"):
        return
    await q.answer()
    rtype = data.split(":", 1)[1]
    ctx.user_data.setdefault("new_code", {})["reward_type"] = rtype
    ctx.user_data["adm_action"] = "code_amount"
    await q.edit_message_text(
        f"🎟️ Reward type: `{rtype}`\n{SUB}\n💰 Enter amount (credits or days):",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN TEXT HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = user.id
    text = update.message.text.strip()
    row  = get_user(uid)

    if not row:
        return

    if _check_maintenance(uid):
        await update.message.reply_text("🔧 Maintenance mode active.")
        return

    # ── Single email check ───────────────────────────────────────────────────
    if ctx.user_data.get("awaiting") == "single_email":
        ctx.user_data.pop("awaiting", None)
        email = text.strip()
        if "@" not in email:
            await update.message.reply_text("❌ Invalid email format.", reply_markup=main_menu_kb())
            return
        if not deduct_one_credit(uid):
            await update.message.reply_text("❌ Insufficient credits.", reply_markup=main_menu_kb())
            return
        msg = await update.message.reply_text(f"⚡ Checking `{email}`...", parse_mode="Markdown")
        status = await asyncio.get_running_loop().run_in_executor(None, _check_email, email)
        emoji = STATUS_EMOJI.get(status, "?")
        if status in ("error", "rate_limited", "captcha_error",
                      "timeout", "connection_error", "api_error",
                      "blacklisted", "invalid"):
            refund_one_credit(uid)
        else:
            with get_db() as db:
                db.execute(
                    "UPDATE users SET total_checks=total_checks+1 WHERE telegram_id=?", (uid,)
                )
        updated = get_user(uid)
        await msg.edit_text(
            f"{HEADER}\n{DIV}\n"
            f"🔍 *SINGLE CHECK RESULT*\n{SUB}\n"
            f"📧 `{email}`\n"
            f"{emoji} Status: *{status.replace('_', ' ').title()}*\n\n"
            f"💰 Credits remaining: `{updated['credits']}`",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return

    # ── Redeem code ──────────────────────────────────────────────────────────
    if ctx.user_data.get("awaiting") == "redeem_code":
        ctx.user_data.pop("awaiting", None)
        if row["status"] != "APPROVED":
            return
        success, msg_text = claim_redeem_code(uid, text)
        updated = get_user(uid)
        await update.message.reply_text(
            f"{HEADER}\n{DIV}\n"
            f"🎟️ *REDEEM CODE*\n{SUB}\n"
            f"{msg_text}\n\n"
            f"💰 Balance: `{updated['credits']}`",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return

    # ── Admin text flows ─────────────────────────────────────────────────────
    if uid == OWNER_ID and ctx.user_data.get("adm_action"):
        await _handle_admin_text(update, ctx)
        return

    # ── Owner bulk paste ─────────────────────────────────────────────────────
    if uid == OWNER_ID and "@" in text:
        emails = _parse_emails(text)
        if emails and len(emails) <= 200:
            results = []
            msg = await update.message.reply_text(f"⚡ Checking {len(emails)} email(s)...")
            _loop2 = asyncio.get_running_loop()
            for _e2 in emails:
                _s2 = await _loop2.run_in_executor(None, _check_email, _e2)
                results.append((_e2, _s2))
                await asyncio.sleep(0.5)
            avail  = [e for e, s in results if s == "available"]
            ctx.user_data["last_available_emails"] = avail
            lines  = [f"{STATUS_EMOJI.get(s,'?')} `{e}` — {s}" for e, s in results]
            summary = (
                f"✅ {len(avail)} avail | ❌ {sum(1 for _,s in results if s=='taken')} taken | "
                f"⚠️ {sum(1 for _,s in results if s not in ('available','taken'))} other"
            )
            await msg.edit_text(
                f"📊 *RESULTS*\n{DIV}\n{chr(10).join(lines)[:3500]}\n{DIV}\n{summary}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    btn("COPY AVAILABLE", "copy_bulk:memory", STYLE_SUCCESS)
                ]]) if avail else None,
            )
            return


# ══════════════════════════════════════════════════════════════════════════════
#  HEALTH SERVER  (async — runs inside PTB's event loop, Python 3.14 safe)
# ══════════════════════════════════════════════════════════════════════════════

async def _health_handler(reader, writer):
    """Minimal async HTTP handler — just returns 200 OK for Render health checks."""
    try:
        await reader.read(1024)  # consume request
    except Exception:
        pass
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b"OK"
    )
    writer.write(response)
    await writer.drain()
    writer.close()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def _build_app() -> "Application":
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("admin",  cmd_admin))
    app.add_handler(CommandHandler("live",   cmd_live))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("help",   lambda u, c: u.message.reply_text(
        f"{HEADER}\n{DIV}\n"
        "📖 *Commands*\n"
        "  ▸ /start — Main menu\n"
        "  ▸ /admin — Owner panel\n"
        "  ▸ /check — Quick check (owner)\n"
        "  ▸ /cancel — Cancel current action\n\n"
        "💡 Upload a .txt file to bulk check emails.",
        parse_mode="Markdown",
    )))
    app.add_handler(CallbackQueryHandler(callback_code_type, pattern=r"^_codetype:"))
    app.add_handler(CallbackQueryHandler(callback_router))
    # chat_member updates ke liye bot ko channels me ADMIN hona chahiye
    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.Document.MimeType("text/plain"), handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


async def _run():
    """Python 3.14 safe — health server + PTB both run inside asyncio.run()."""
    import signal as _signal

    # Start async health server INSIDE the event loop (no threading)
    port = int(os.environ.get("PORT", "10000"))
    health_srv = await asyncio.start_server(_health_handler, "0.0.0.0", port)
    log.info("Health server on port %s", port)

    # Build and start PTB app
    app = _build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    log.info("Bot polling started (chat_member updates ON).")

    # Background rewards jobs (referral lock sweep / weekly top / auto-backup)
    jobs_task = asyncio.create_task(_jobs_loop(app))

    # Wait for shutdown signal
    stop_event = asyncio.Event()

    def _stop(*_):
        stop_event.set()

    for sig in (getattr(_signal, "SIGINT", None), getattr(_signal, "SIGTERM", None)):
        if sig:
            try:
                asyncio.get_running_loop().add_signal_handler(sig, _stop)
            except (NotImplementedError, RuntimeError):
                pass

    await stop_event.wait()

    # Graceful shutdown
    jobs_task.cancel()
    health_srv.close()
    await health_srv.wait_closed()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


def main():
    init_db()
    _rebuild_proxy_cycle()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
