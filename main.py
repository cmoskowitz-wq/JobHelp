#!/usr/bin/env python3
"""
main.py — JobHelp Version 2
Entry point: loads config, runs all scrapers via headless browser,
applies smart time-window + deduplication, emails the digest,
and optionally keeps running on a schedule.

Usage:
  python main.py              # run once immediately, then schedule
  python main.py --now        # run immediately and exit
  python main.py --dry-run    # run scrapers, print report, don't send email
  python main.py --list-boards
"""

from __future__ import annotations

import argparse
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
from playwright.sync_api import sync_playwright

from email_sender import build_html_report, send_report
from scrapers import Job, build_scrapers, deduplicate
from state import filter_seen, get_hours_window, mark_seen, record_run

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

    email = cfg.setdefault("email", {})
    if os.getenv("EMAIL_SENDER"):
        email["sender"] = os.environ["EMAIL_SENDER"]
    if os.getenv("EMAIL_PASSWORD"):
        email["password"] = os.environ["EMAIL_PASSWORD"]

    boards = cfg.setdefault("job_boards", {})
    adzuna = boards.setdefault("adzuna", {})
    if os.getenv("ADZUNA_APP_ID"):
        adzuna["app_id"] = os.environ["ADZUNA_APP_ID"]
    if os.getenv("ADZUNA_APP_KEY"):
        adzuna["app_key"] = os.environ["ADZUNA_APP_KEY"]

    return cfg


# ── Core job ──────────────────────────────────────────────────────────────────

def run_job(cfg: dict, dry_run: bool = False) -> list[Job]:
    """
    Run all scrapers, apply smart time-window + state-based dedup,
    email the digest (unless dry_run), and persist state.
    """
    job_titles: list[str] = cfg.get("job_titles", [])
    if not job_titles:
        logger.warning("No job titles configured — nothing to search.")
        return []

    # ── Determine search window ───────────────────────────────────────────────
    hours_window = get_hours_window()
    logger.info("Search window: last %d hour(s).", hours_window)

    # Inject the dynamic window into a copy of config (don't mutate original)
    cfg = {**cfg, "search": {**cfg.get("search", {}), "hours_ago": hours_window}}

    # ── Launch browser + scrape ───────────────────────────────────────────────
    all_jobs: list[Job] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            scrapers = build_scrapers(cfg, browser)
            if not scrapers:
                logger.warning("No job boards enabled — check config.yaml.")
                return []

            logger.info(
                "JobHelp v2 — %d board(s) × %d title(s)",
                len(scrapers), len(job_titles),
            )

            for scraper in scrapers:
                logger.info("  Scraping %s ...", scraper.name)
                jobs = scraper.search_all(job_titles)
                logger.info("    → %d result(s)", len(jobs))
                all_jobs.extend(jobs)
        finally:
            browser.close()

    # ── Dedup: cross-board (same title+company) ───────────────────────────────
    all_jobs = deduplicate(all_jobs)
    logger.info("After cross-board dedup: %d unique job(s).", len(all_jobs))

    # ── Dedup: cross-run (already seen today) ─────────────────────────────────
    all_jobs = filter_seen(all_jobs)
    logger.info("After state filter: %d new job(s) to report.", len(all_jobs))

    # ── Deliver ───────────────────────────────────────────────────────────────
    if dry_run:
        _print_dry_run(all_jobs, cfg, hours_window)
    else:
        if send_report(all_jobs, cfg):
            mark_seen(all_jobs)
            record_run()
        else:
            logger.error("Email failed — state not updated.")

    return all_jobs


# ── Dry-run printer ───────────────────────────────────────────────────────────

def _print_dry_run(jobs: list[Job], cfg: dict, hours_window: int) -> None:
    print("\n" + "=" * 70)
    print(f"DRY RUN — would email {len(jobs)} job(s) from last {hours_window}h")
    print("=" * 70)

    by_title: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        by_title[job.search_term].append(job)

    for title in cfg.get("job_titles", []):
        title_jobs = by_title.get(title, [])
        print(f"\n{title.upper()} ({len(title_jobs)} results)")
        print("─" * 50)
        for job in title_jobs[:5]:
            posted_str = ""
            if job.posted:
                p = job.posted if job.posted.tzinfo else job.posted.replace(tzinfo=timezone.utc)
                hours = int((datetime.now(timezone.utc) - p).total_seconds() / 3600)
                posted_str = f" [{hours}h ago]"
            print(f"  • {job.title} @ {job.company} ({job.source}){posted_str}")
            if job.url:
                print(f"    {job.url}")
        if len(title_jobs) > 5:
            print(f"  ... and {len(title_jobs) - 5} more")

    print("\n" + "=" * 70 + "\n")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler(cfg: dict) -> None:
    """Block forever, running run_job on the configured schedule."""
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
        description="JobHelp Version 2 — Tech leadership job digest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py              Run once then stay scheduled (daemon mode)
  python main.py --now        Scrape and email right now, then exit
  python main.py --dry-run    Scrape and print results, do NOT email
  python main.py --config /path/to/other.yaml   Use a custom config file
""",
    )
    p.add_argument("--now", action="store_true",
                   help="Run once immediately and exit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Scrape but print results instead of emailing.")
    p.add_argument("--config", default=None,
                   help="Path to an alternative config YAML file.")
    p.add_argument("--list-boards", action="store_true",
                   help="List all supported job boards and exit.")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = Path(args.config)

    if args.list_boards:
        from scrapers import SCRAPER_REGISTRY
        print("\nSupported job boards:")
        for key, cls in SCRAPER_REGISTRY.items():
            print(f"  {key:<18} → {cls.name}")
        print()
        sys.exit(0)

    try:
        cfg = load_config()
    except FileNotFoundError:
        logger.error("config.yaml not found at %s", CONFIG_PATH)
        sys.exit(1)
    except yaml.YAMLError as exc:
        logger.error("Invalid config.yaml: %s", exc)
        sys.exit(1)

    logger.info("Loaded config: %s", cfg.get("version", "JobHelp v2"))

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
