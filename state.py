"""
state.py — JobHelp Version 3
SQLite-backed state management.

Tables
──────
runs          — per-run timestamps (smart time-window)
seen_jobs     — cross-run deduplication (today's seen jobs)
job_cache     — full job objects stored for the web dashboard
applications  — application tracker (status, notes)
board_stats   — per-board last-scraped timestamp (LinkedIn cooldown)

Public API additions (v3)
──────────────────────────
save_jobs_to_cache(jobs)           — persist jobs for the dashboard
get_cached_jobs(run_date=None)     — retrieve stored jobs
get_job_by_key(job_key)            — single job lookup
update_application(job_key, status, notes)
get_applications()                 — list all tracked applications
get_board_last_scraped(board_name) — for per-board cooldown checks
update_board_stats(board_name, success)
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from scrapers import Job

logger = logging.getLogger(__name__)

STATE_DB = Path(__file__).with_name("jobhelp_state.db")

APPLICATION_STATUSES = ("new", "interested", "applied", "interviewing", "offer", "rejected")


# ── DB bootstrap ──────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            run_ts   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_key   TEXT NOT NULL,
            seen_date TEXT NOT NULL,
            PRIMARY KEY (job_key, seen_date)
        );

        CREATE TABLE IF NOT EXISTS job_cache (
            job_key    TEXT NOT NULL,
            run_date   TEXT NOT NULL,
            run_ts     TEXT,            -- ISO-8601 UTC timestamp of the run
            data       TEXT NOT NULL,   -- JSON blob
            PRIMARY KEY (job_key, run_date)
        );

        CREATE TABLE IF NOT EXISTS applications (
            job_key    TEXT NOT NULL PRIMARY KEY,
            title      TEXT,
            company    TEXT,
            location   TEXT,
            url        TEXT,
            status     TEXT DEFAULT 'new',
            notes      TEXT DEFAULT '',
            added_ts   TEXT NOT NULL,
            updated_ts TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS board_stats (
            board_name     TEXT NOT NULL PRIMARY KEY,
            last_scraped   TEXT,        -- ISO-8601 UTC
            success_count  INTEGER DEFAULT 0,
            fail_count     INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    # Migrate existing databases that lack the run_ts column
    try:
        conn.execute("ALTER TABLE job_cache ADD COLUMN run_ts TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


# ── Key helper ────────────────────────────────────────────────────────────────

def _job_key(title: str, company: str) -> str:
    raw = f"{title.strip().lower()}||{company.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Existing v2 API (unchanged) ───────────────────────────────────────────────

def get_hours_window() -> int:
    today = date.today().isoformat()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT run_ts FROM runs WHERE run_date = ? ORDER BY run_ts DESC LIMIT 1",
            (today,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        logger.info("[state] No prior run today — using 24h window.")
        return 24

    last_run = datetime.fromisoformat(row["run_ts"])
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)

    elapsed_hours = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
    window = max(1, round(elapsed_hours))
    logger.info("[state] Last run was %.1fh ago — using %dh window.", elapsed_hours, window)
    return window


def filter_seen(jobs: List[Job]) -> List[Job]:
    today = date.today().isoformat()
    conn = _connect()
    try:
        new: List[Job] = []
        for job in jobs:
            key = _job_key(job.title, job.company)
            if not conn.execute(
                "SELECT 1 FROM seen_jobs WHERE job_key = ? AND seen_date = ?",
                (key, today),
            ).fetchone():
                new.append(job)
        skipped = len(jobs) - len(new)
        if skipped:
            logger.info("[state] Filtered %d already-seen job(s).", skipped)
        return new
    finally:
        conn.close()


def mark_seen(jobs: List[Job]) -> None:
    today = date.today().isoformat()
    conn = _connect()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO seen_jobs (job_key, seen_date) VALUES (?, ?)",
            [(_job_key(j.title, j.company), today) for j in jobs],
        )
        conn.commit()
    finally:
        conn.close()


def record_run() -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO runs (run_date, run_ts) VALUES (?, ?)",
            (date.today().isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("[state] Run recorded.")


# ── v3: Job cache (for dashboard) ─────────────────────────────────────────────

def save_jobs_to_cache(jobs: List[Job]) -> None:
    """Persist the current run's jobs for the web dashboard."""
    today = date.today().isoformat()
    run_ts = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        rows = []
        for job in jobs:
            key = _job_key(job.title, job.company)
            data = json.dumps(job.to_dict())
            rows.append((key, today, run_ts, data))
        conn.executemany(
            "INSERT OR REPLACE INTO job_cache (job_key, run_date, run_ts, data) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        logger.info("[state] Cached %d job(s) for dashboard (run_ts=%s).", len(jobs), run_ts)
    finally:
        conn.close()


def get_cached_jobs(run_date: Optional[str] = None, latest_run_only: bool = True) -> list[dict]:
    """Return jobs from the cache for the given date (default: today).

    When latest_run_only=True (the default), only jobs from the most recent
    run for that date are returned — matching what was sent in the last email.
    Set latest_run_only=False to see all accumulated jobs for the day.
    """
    target = run_date or date.today().isoformat()
    conn = _connect()
    try:
        if latest_run_only:
            # Find the most recent run_ts for this date
            ts_row = conn.execute(
                "SELECT MAX(run_ts) as latest FROM job_cache WHERE run_date = ?",
                (target,),
            ).fetchone()
            latest_ts = ts_row["latest"] if ts_row else None
            if latest_ts:
                rows = conn.execute(
                    "SELECT data FROM job_cache WHERE run_date = ? AND run_ts = ? ORDER BY rowid",
                    (target, latest_ts),
                ).fetchall()
            else:
                # Fallback for legacy rows without run_ts
                rows = conn.execute(
                    "SELECT data FROM job_cache WHERE run_date = ? ORDER BY rowid",
                    (target,),
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT data FROM job_cache WHERE run_date = ? ORDER BY rowid",
                (target,),
            ).fetchall()
        jobs = []
        for row in rows:
            try:
                job_dict = json.loads(row["data"])
                job_dict["job_key"] = _job_key(
                    job_dict.get("title", ""), job_dict.get("company", "")
                )
                jobs.append(job_dict)
            except (json.JSONDecodeError, KeyError):
                pass
        return jobs
    finally:
        conn.close()


def get_available_dates() -> list[str]:
    """Return all dates that have cached jobs, most recent first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT run_date FROM job_cache ORDER BY run_date DESC"
        ).fetchall()
        return [r["run_date"] for r in rows]
    finally:
        conn.close()


def get_job_by_key(job_key: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT data FROM job_cache WHERE job_key = ? ORDER BY run_date DESC LIMIT 1",
            (job_key,),
        ).fetchone()
        if row:
            d = json.loads(row["data"])
            d["job_key"] = job_key
            return d
        return None
    finally:
        conn.close()


# ── v3: Application tracker ────────────────────────────────────────────────────

def update_application(job_key: str, status: str, notes: str = "") -> None:
    """Insert or update an application record."""
    if status not in APPLICATION_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Choose from: {APPLICATION_STATUSES}")

    now = datetime.now(timezone.utc).isoformat()
    job = get_job_by_key(job_key)
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT job_key FROM applications WHERE job_key = ?", (job_key,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE applications SET status = ?, notes = ?, updated_ts = ? WHERE job_key = ?",
                (status, notes, now, job_key),
            )
        else:
            conn.execute(
                """INSERT INTO applications
                   (job_key, title, company, location, url, status, notes, added_ts, updated_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_key,
                    job.get("title", "") if job else "",
                    job.get("company", "") if job else "",
                    job.get("location", "") if job else "",
                    job.get("url", "") if job else "",
                    status, notes, now, now,
                ),
            )
        conn.commit()
        logger.info("[state] Application %s → status='%s'.", job_key[:8], status)
    finally:
        conn.close()


def get_applications(status_filter: Optional[str] = None) -> list[dict]:
    """Return all application records, optionally filtered by status."""
    conn = _connect()
    try:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM applications WHERE status = ? ORDER BY updated_ts DESC",
                (status_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM applications ORDER BY updated_ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_application(job_key: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM applications WHERE job_key = ?", (job_key,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── v3: Per-board cooldown (prevents LinkedIn hammering) ──────────────────────

def get_board_last_scraped(board_name: str) -> Optional[datetime]:
    """Return the datetime of the last successful scrape for this board, or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT last_scraped FROM board_stats WHERE board_name = ?",
            (board_name,),
        ).fetchone()
        if row and row["last_scraped"]:
            ts = datetime.fromisoformat(row["last_scraped"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        return None
    finally:
        conn.close()


def update_board_stats(board_name: str, success: bool = True) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT board_name FROM board_stats WHERE board_name = ?", (board_name,)
        ).fetchone()
        if existing:
            if success:
                conn.execute(
                    """UPDATE board_stats
                       SET last_scraped = ?, success_count = success_count + 1
                       WHERE board_name = ?""",
                    (now, board_name),
                )
            else:
                conn.execute(
                    "UPDATE board_stats SET fail_count = fail_count + 1 WHERE board_name = ?",
                    (board_name,),
                )
        else:
            conn.execute(
                """INSERT INTO board_stats (board_name, last_scraped, success_count, fail_count)
                   VALUES (?, ?, ?, ?)""",
                (board_name, now if success else None, 1 if success else 0, 0 if success else 1),
            )
        conn.commit()
    finally:
        conn.close()
