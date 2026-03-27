"""
notifier.py — JobHelp Version 3
Optional Slack and SMS (Twilio) notifications for new job results.

Config block (config.yaml):
  notifications:
    slack:
      enabled: true
      webhook_url: ""          # or set SLACK_WEBHOOK_URL env var
      max_jobs: 10             # top N jobs to mention (by AI score, then recency)
    sms:
      enabled: false
      account_sid: ""          # or set TWILIO_ACCOUNT_SID env var
      auth_token: ""           # or set TWILIO_AUTH_TOKEN env var
      from_number: "+15551234567"
      to_number: "+15557654321"
      max_jobs: 5

Both integrations fail silently with a warning log if credentials are absent
or if the send fails — they never crash the main flow.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List

import requests

if TYPE_CHECKING:
    from scrapers import Job

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _top_jobs(jobs: List[Job], n: int) -> List[Job]:
    """Return the top-N jobs sorted by AI score (desc) then recency (desc)."""
    def _key(j: Job):
        score = j.ai_score if j.ai_score is not None else 5.0
        ts = j.posted or datetime.min.replace(tzinfo=timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (-score, -ts.timestamp())

    return sorted(jobs, key=_key)[:n]


def _fmt_salary(job: Job) -> str:
    if job.salary_text:
        return f" | {job.salary_text}"
    if job.salary_min:
        lo = f"${job.salary_min:,}"
        hi = f" – ${job.salary_max:,}" if job.salary_max and job.salary_max != job.salary_min else ""
        return f" | {lo}{hi}"
    return ""


# ── Slack ─────────────────────────────────────────────────────────────────────

def send_slack_notification(jobs: List[Job], config: dict) -> bool:
    """
    Post a brief Slack message via an Incoming Webhook.
    Returns True on success.
    """
    slack_cfg = config.get("notifications", {}).get("slack", {})
    if not slack_cfg.get("enabled", False):
        return False

    webhook = slack_cfg.get("webhook_url", "") or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook:
        logger.warning("[Slack] No webhook_url configured — skipping.")
        return False

    max_jobs = int(slack_cfg.get("max_jobs", 10))
    top = _top_jobs(jobs, max_jobs)

    if not top:
        return False

    geo_count = sum(1 for j in jobs if j.geo_priority)
    header = (
        f"*JobHelp v3 — {len(jobs)} new job(s)*"
        + (f"  _(incl. {geo_count} NJ/CT/NYC)_" if geo_count else "")
    )

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
    ]

    for job in top:
        score_badge = f"  ★{job.ai_score:.0f}" if job.ai_score is not None else ""
        geo_badge = "  📍" if job.geo_priority else ""
        sal = _fmt_salary(job)
        title_line = (
            f"*<{job.url}|{job.title}>*" if job.url
            else f"*{job.title}*"
        )
        body = (
            f"{title_line}{score_badge}{geo_badge}\n"
            f"{job.company} — {job.location}{sal}"
        )
        if job.ai_summary:
            body += f"\n_{job.ai_summary}_"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        })

    if len(jobs) > max_jobs:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"…and {len(jobs) - max_jobs} more in the email digest."}],
        })

    payload = {"blocks": blocks}
    try:
        resp = requests.post(
            webhook,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("[Slack] Notification sent (%d jobs).", len(top))
        return True
    except requests.RequestException as exc:
        logger.warning("[Slack] Send failed: %s", exc)
        return False


# ── SMS (Twilio) ──────────────────────────────────────────────────────────────

def send_sms_notification(jobs: List[Job], config: dict) -> bool:
    """
    Send a brief SMS via Twilio.
    Returns True on success.
    """
    sms_cfg = config.get("notifications", {}).get("sms", {})
    if not sms_cfg.get("enabled", False):
        return False

    account_sid = sms_cfg.get("account_sid", "") or os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token = sms_cfg.get("auth_token", "") or os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = sms_cfg.get("from_number", "") or os.environ.get("TWILIO_FROM_NUMBER", "")
    to_number = sms_cfg.get("to_number", "") or os.environ.get("TWILIO_TO_NUMBER", "")

    if not all([account_sid, auth_token, from_number, to_number]):
        logger.warning("[SMS] Twilio credentials incomplete — skipping.")
        return False

    max_jobs = int(sms_cfg.get("max_jobs", 5))
    top = _top_jobs(jobs, max_jobs)

    if not top:
        return False

    geo_count = sum(1 for j in jobs if j.geo_priority)
    lines = [f"JobHelp: {len(jobs)} new jobs" + (f" ({geo_count} NJ/CT/NYC)" if geo_count else "")]
    for job in top:
        sal = _fmt_salary(job)
        lines.append(f"• {job.title} @ {job.company} [{job.location}]{sal}")
    body = "\n".join(lines)

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        message = client.messages.create(body=body, from_=from_number, to=to_number)
        logger.info("[SMS] Sent SID=%s to %s.", message.sid, to_number)
        return True
    except ImportError:
        logger.warning("[SMS] twilio package not installed — run: pip install twilio")
    except Exception as exc:
        logger.warning("[SMS] Send failed: %s", exc)
    return False
