#!/usr/bin/env python3
"""
main.py — JobHelp Version 3
Entry point: loads config, runs all scrapers in parallel (async Playwright),
applies smart time-window + fuzzy deduplication, geo-priority sorts NJ/CT/NYC
jobs to the top, optionally scores with Claude AI, emails the digest, fires
Slack/SMS notifications, persists state, and provides a web dashboard.

Usage:
  python main.py                   # run once immediately, then schedule
  python main.py --now             # run immediately and exit
  python main.py --dry-run         # run scrapers, print report, don't send email
  python main.py --dashboard       # launch the web dashboard (port 5000)
  python main.py --list-boards     # list all supported job boards and exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import schedule
import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from ai_scorer import score_jobs
from email_sender import build_html_report, send_report
from notifier import send_slack_notification, send_sms_notification
from scrapers import (
    Job,
    fuzzy_deduplicate,
    geo_sort,
    run_all_scrapers_async,
)
from state import (
    filter_seen,
    get_hours_window,
    mark_seen,
    record_run,
    reset_today,
    save_jobs_to_cache,
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("jobhelp")

# ── Config loader ─────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_config() -> dict:
    """Load YAML config and overlay secrets from .env / environment."""
    load_dotenv()

    with open(CONFIG_PATH, "r") as fh:
        cfg = yaml.safe_load(fh)

    # Email credentials
    email = cfg.setdefault("email", {})
    if os.getenv("EMAIL_SENDER"):
        email["sender"] = os.environ["EMAIL_SENDER"]
    if os.getenv("EMAIL_PASSWORD"):
        email["password"] = os.environ["EMAIL_PASSWORD"]

    # Adzuna API keys
    boards = cfg.setdefault("job_boards", {})
    adzuna = boards.setdefault("adzuna", {})
    if os.getenv("ADZUNA_APP_ID"):
        adzuna["app_id"] = os.environ["ADZUNA_APP_ID"]
    if os.getenv("ADZUNA_APP_KEY"):
        adzuna["app_key"] = os.environ["ADZUNA_APP_KEY"]

    # Slack / SMS tokens are read directly from env in notifier.py
    return cfg


# ── Async core job ────────────────────────────────────────────────────────────

async def _run_job_async(cfg: dict, dry_run: bool = False) -> list[Job]:
    """
    Async implementation:
      1. Determine smart time-window
      2. Launch headless browser
      3. Run ALL scrapers in parallel via asyncio.gather
      4. Fuzzy dedup + state filter
      5. Geo-sort (NJ/CT/NYC first)
      6. AI score (optional)
      7. Cache for dashboard
      8. Email + Slack/SMS (unless dry_run)
    """
    job_titles: list[str] = cfg.get("job_titles", [])
    if not job_titles:
        logger.warning("No job titles configured — nothing to search.")
        return []

    # ── Smart time-window ─────────────────────────────────────────────────────
    hours_window = get_hours_window()
    logger.info("Search window: last %d hour(s).", hours_window)
    cfg = {**cfg, "search": {**cfg.get("search", {}), "hours_ago": hours_window}}

    # ── Parallel scraping ─────────────────────────────────────────────────────
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            all_jobs, board_counts = await run_all_scrapers_async(cfg, browser, job_titles)
        finally:
            await browser.close()

    # ── Fuzzy cross-board dedup ───────────────────────────────────────────────
    all_jobs = fuzzy_deduplicate(all_jobs)
    logger.info("After fuzzy dedup: %d unique job(s).", len(all_jobs))

    # ── Cross-run dedup (seen today or yesterday) ─────────────────────────────
    all_jobs = filter_seen(all_jobs)
    logger.info("After state filter: %d new job(s) to report.", len(all_jobs))

    if not all_jobs:
        logger.info("No new jobs to report.")
        return []

    # ── Keyword exclusion filter ───────────────────────────────────────────────
    filters_cfg = cfg.get("filters", {})
    exclude_kw = [kw.lower() for kw in filters_cfg.get("exclude_title_keywords", [])]
    if exclude_kw:
        before = len(all_jobs)
        all_jobs = [
            j for j in all_jobs
            if not any(kw in j.title.lower() for kw in exclude_kw)
        ]
        removed = before - len(all_jobs)
        if removed:
            logger.info("Keyword filter: removed %d job(s) matching exclusion list.", removed)

    # ── Minimum salary filter ──────────────────────────────────────────────────
    min_salary = filters_cfg.get("min_salary", 0)
    if min_salary > 0:
        before = len(all_jobs)
        all_jobs = [
            j for j in all_jobs
            if not (j.salary_max is not None and j.salary_max < min_salary)
        ]
        removed = before - len(all_jobs)
        if removed:
            logger.info(
                "Salary filter: removed %d job(s) with max salary below $%s.",
                removed, f"{min_salary:,}",
            )

    # ── Geo-priority sort (NJ/CT/NYC float to top) ────────────────────────────
    geo_regions = cfg.get("geo_priority", {}).get("regions", None)
    all_jobs = geo_sort(all_jobs, geo_regions)
    geo_count = sum(1 for j in all_jobs if j.geo_priority)
    if geo_count:
        logger.info("Geo-priority: %d NJ/CT/NYC job(s) sorted to top.", geo_count)

    # ── AI scoring (optional) ─────────────────────────────────────────────────
    if cfg.get("ai", {}).get("enabled", False):
        all_jobs = score_jobs(all_jobs, cfg)

    # ── Persist for dashboard ─────────────────────────────────────────────────
    save_jobs_to_cache(all_jobs)

    # ── Deliver ───────────────────────────────────────────────────────────────
    if dry_run:
        _print_dry_run(all_jobs, cfg, hours_window)
    else:
        if send_report(all_jobs, cfg, board_counts=board_counts):
            send_slack_notification(all_jobs, cfg)
            send_sms_notification(all_jobs, cfg)
            mark_seen(all_jobs)
            record_run()
        else:
            logger.error("Email failed — state not updated.")

    return all_jobs


def run_job(cfg: dict, dry_run: bool = False) -> list[Job]:
    """Synchronous wrapper — called by the scheduler and CLI."""
    return asyncio.run(_run_job_async(cfg, dry_run))


# ── Dry-run printer ───────────────────────────────────────────────────────────

def _print_dry_run(jobs: list[Job], cfg: dict, hours_window: int) -> None:
    print("\n" + "=" * 70)
    print(f"DRY RUN — would email {len(jobs)} job(s) from last {hours_window}h")
    geo_count = sum(1 for j in jobs if j.geo_priority)
    if geo_count:
        print(f"           ({geo_count} NJ/CT/NYC geo-priority jobs at top)")
    print("=" * 70)

    by_title: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        by_title[job.search_term].append(job)

    for title in cfg.get("job_titles", []):
        title_jobs = by_title.get(title, [])
        print(f"\n{title.upper()} ({len(title_jobs)} results)")
        print("─" * 50)
        for job in title_jobs[:8]:
            geo_flag = " 📍" if job.geo_priority else ""
            posted_str = ""
            if job.posted:
                p = job.posted if job.posted.tzinfo else job.posted.replace(tzinfo=timezone.utc)
                hours = int((datetime.now(timezone.utc) - p).total_seconds() / 3600)
                posted_str = f" [{hours}h ago]"
            sal = ""
            if job.salary_text:
                sal = f" | {job.salary_text}"
            score = f" | AI:{job.ai_score:.1f}" if job.ai_score is not None else ""
            print(f"  • {job.title} @ {job.company} ({job.source}){geo_flag}{posted_str}{sal}{score}")
            if job.ai_summary:
                print(f"    → {job.ai_summary}")
            if job.url:
                print(f"    {job.url}")
        if len(title_jobs) > 8:
            print(f"  ... and {len(title_jobs) - 8} more")

    print("\n" + "=" * 70 + "\n")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler(cfg: dict) -> None:
    email_cfg = cfg.get("email", {})
    sched = email_cfg.get("schedule", "daily")
    daily_time = email_cfg.get("daily_time", "08:00")

    if sched == "daily":
        logger.info("Scheduling daily digest at %s.", daily_time)
        schedule.every().day.at(daily_time).do(run_job, cfg=cfg)
        run_job(cfg)
    elif sched == "hourly":
        logger.info("Scheduling hourly digest.")
        schedule.every().hour.do(run_job, cfg=cfg)
        run_job(cfg)
    else:
        try:
            h, m = [int(x) for x in sched.split(":")[:2]]
            run_time = f"{h:02d}:{m:02d}"
            logger.info("Scheduling daily digest at %s.", run_time)
            schedule.every().day.at(run_time).do(run_job, cfg=cfg)
        except (ValueError, AttributeError):
            logger.warning(
                "Unrecognised schedule '%s', defaulting to daily at %s.",
                sched, daily_time,
            )
            schedule.every().day.at(daily_time).do(run_job, cfg=cfg)
        run_job(cfg)

    logger.info("Scheduler running. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="JobHelp Version 3 — Tech leadership job digest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                  Run once then stay scheduled (daemon mode)
  python main.py --now            Scrape and email right now, then exit
  python main.py --dry-run        Scrape and print results, do NOT email
  python main.py --dashboard      Open the web dashboard (http://localhost:5000)
  python main.py --config /path/to/other.yaml
""",
    )
    p.add_argument("--now", action="store_true", help="Run once immediately and exit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Scrape but print results instead of emailing.")
    p.add_argument("--config", default=None, help="Path to an alternative config YAML file.")
    p.add_argument("--list-boards", action="store_true",
                   help="List all supported job boards and exit.")
    p.add_argument("--dashboard", action="store_true",
                   help="Launch the web dashboard (http://localhost:5000).")
    p.add_argument("--dashboard-port", type=int, default=5000,
                   help="Port for the web dashboard (default: 5000).")
    p.add_argument("--reset-today", action="store_true",
                   help="Clear today's seen jobs and run history, then exit. Useful for re-testing.")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = Path(args.config)

    if args.reset_today:
        reset_today()
        print("Today's state cleared. Next run will treat today as a fresh start.")
        sys.exit(0)

    if args.list_boards:
        from scrapers import SCRAPER_REGISTRY
        print("\nSupported job boards:")
        for key, cls in SCRAPER_REGISTRY.items():
            print(f"  {key:<18} → {cls.name}")
        print()
        sys.exit(0)

    if args.dashboard:
        from dashboard import run_dashboard
        run_dashboard(port=args.dashboard_port)
        return

    try:
        cfg = load_config()
    except FileNotFoundError:
        logger.error("config.yaml not found at %s", CONFIG_PATH)
        sys.exit(1)
    except yaml.YAMLError as exc:
        logger.error("Invalid config.yaml: %s", exc)
        sys.exit(1)

    logger.info("JobHelp v4 — %s", cfg.get("version", "JobHelp Version 4"))

    if args.now:
        run_job(cfg, dry_run=args.dry_run)
    elif args.dry_run:
        run_job(cfg, dry_run=True)
    else:
        try:
            start_scheduler(cfg)
        except KeyboardInterrupt:
            logger.info("JobHelp stopped by user.")


if __name__ == "__main__":
    main()
