"""
state.py — JobHelp Version 2
SQLite-backed run tracking and cross-run job deduplication.

Rules:
  - First run ever, or first run today (after midnight) → 24h window
  - Subsequent runs same day → hours since last run today
  - Jobs seen in a previous run today are not emailed again
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from scrapers import Job

logger = logging.getLogger(__name__)

STATE_DB = Path(__file__).with_name("jobhelp_state.db")


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
            run_date TEXT NOT NULL,   -- YYYY-MM-DD
            run_ts   TEXT NOT NULL    -- ISO-8601 UTC
        );
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_key   TEXT NOT NULL,  -- sha256(title||company)
            seen_date TEXT NOT NULL,  -- YYYY-MM-DD
            PRIMARY KEY (job_key, seen_date)
        );
    """)
    conn.commit()


# ── Key helper ────────────────────────────────────────────────────────────────

def _job_key(title: str, company: str) -> str:
    raw = f"{title.strip().lower()}||{company.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Public API ────────────────────────────────────────────────────────────────

def get_hours_window() -> int:
    """
    Return how many hours back to search.

    - No runs today (or ever)  → 24
    - Already ran today        → hours elapsed since the last run (min 1)
    """
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
    """Return only jobs not already seen today."""
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
    """Persist jobs as seen today so they won't appear in later runs."""
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
    """Persist the current run timestamp."""
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
