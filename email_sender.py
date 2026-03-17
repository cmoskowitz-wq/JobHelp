"""
email_sender.py — JobHelp Version 1
Formats job results as an HTML email digest and sends via SMTP.
"""

from __future__ import annotations

import logging
import smtplib
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

from scrapers import Job

logger = logging.getLogger(__name__)

# ── HTML template pieces ──────────────────────────────────────────────────────

_HTML_HEAD = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JobHelp — Daily Digest</title>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
             background:#f4f6f9;color:#333;margin:0;padding:0;">
<div style="max-width:600px;margin:0 auto;background:#f4f6f9;">
"""

_HTML_FOOT = """\
  <div style="background:#f8fafc;padding:16px 20px;font-size:12px;color:#aaa;
              border-top:1px solid #e2e8f0;text-align:center;">
    JobHelp &bull; Generated {generated} UTC &bull;
    Edit <code>config.yaml</code> to adjust searches.
  </div>
</div>
</body>
</html>
"""


def _fmt_posted(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    hours = int(delta.total_seconds() / 3600)
    if hours < 1:
        return "Just now"
    if hours < 24:
        return f"{hours}h ago"
    return dt.strftime("%b %d")


def _escape(text: str) -> str:
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ── Report builder ────────────────────────────────────────────────────────────

def _sort_key(j: Job) -> datetime:
    if j.posted is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if j.posted.tzinfo is None:
        return j.posted.replace(tzinfo=timezone.utc)
    return j.posted


def _job_card(job: Job) -> str:
    """Render a single job as an inline-styled card (works in all email clients)."""
    if job.url:
        title_html = (
            f'<a href="{_escape(job.url)}" target="_blank" '
            f'style="color:#2563eb;font-weight:700;font-size:16px;'
            f'text-decoration:none;line-height:1.4;">'
            f'{_escape(job.title)}</a>'
        )
    else:
        title_html = (
            f'<span style="font-weight:700;font-size:16px;color:#1e3a5f;">'
            f'{_escape(job.title)}</span>'
        )

    remote_badge = ""
    if job.remote:
        remote_badge = (
            '<span style="background:#dcfce7;color:#166534;font-size:10px;'
            'font-weight:700;padding:2px 7px;border-radius:4px;'
            'margin-left:8px;text-transform:uppercase;vertical-align:middle;">'
            'Remote</span>'
        )

    board_badge = (
        f'<span style="background:#e8edf5;color:#1e3a5f;font-size:11px;'
        f'font-weight:600;padding:2px 8px;border-radius:12px;'
        f'text-transform:uppercase;letter-spacing:.5px;">'
        f'{_escape(job.source)}</span>'
    )

    posted_str = _fmt_posted(job.posted)

    return (
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;'
        f'margin:8px 16px;padding:14px 16px;">'
        f'  <div style="line-height:1.4;">{title_html}{remote_badge}</div>'
        f'  <div style="color:#555;font-size:13px;margin-top:6px;">{_escape(job.company)}</div>'
        f'  <div style="color:#888;font-size:12px;margin-top:2px;">{_escape(job.location)}</div>'
        f'  <div style="margin-top:10px;">'
        f'    {board_badge}'
        f'    <span style="color:#aaa;font-size:12px;margin-left:10px;">{posted_str}</span>'
        f'  </div>'
        f'</div>\n'
    )


def build_html_report(jobs: List[Job], config: dict) -> str:
    """Return a complete HTML email body using inline-styled cards."""
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    hours = config.get("search", {}).get("hours_ago", 24)
    job_titles = config.get("job_titles", [])

    # Group by search_term
    by_title: dict[str, List[Job]] = defaultdict(list)
    for job in jobs:
        by_title[job.search_term].append(job)

    html = _HTML_HEAD

    # ── Header ────────────────────────────────────────────────────────────────
    html += (
        f'<div style="background:linear-gradient(135deg,#1e3a5f 0%,#2563eb 100%);'
        f'color:#fff;padding:24px 20px;">'
        f'  <div style="font-size:22px;font-weight:700;margin:0 0 6px;">&#128188; JobHelp &mdash; Daily Digest</div>'
        f'  <div style="opacity:.85;font-size:13px;">Last {hours}h &bull; {now_str} UTC &bull; {len(jobs)} jobs found</div>'
        f'</div>\n'
    )

    # ── Summary bar ───────────────────────────────────────────────────────────
    boards = len({j.source for j in jobs})
    html += (
        f'<div style="background:#f0f4ff;border-bottom:1px solid #dbe4f5;'
        f'padding:12px 20px;font-size:13px;color:#555;">'
        f'  <span style="margin-right:20px;">Total: <strong style="color:#1e3a5f;">{len(jobs)}</strong></span>'
        f'  <span style="margin-right:20px;">Boards: <strong style="color:#1e3a5f;">{boards}</strong></span>'
        f'  <span>Titles: <strong style="color:#1e3a5f;">{len(job_titles)}</strong></span>'
        f'</div>\n'
    )

    # ── Section per job title ─────────────────────────────────────────────────
    for title in job_titles:
        title_jobs = sorted(by_title.get(title, []), key=_sort_key, reverse=True)
        count = len(title_jobs)

        html += (
            f'<div style="font-size:17px;font-weight:700;color:#1e3a5f;'
            f'padding:20px 20px 10px;border-bottom:2px solid #e8edf5;">'
            f'  {_escape(title)}'
            f'  <span style="font-size:13px;font-weight:400;color:#888;margin-left:8px;">({count})</span>'
            f'</div>\n'
        )

        if not title_jobs:
            html += (
                '<div style="padding:16px 20px;color:#999;font-style:italic;font-size:14px;">'
                'No new results in this window.</div>\n'
            )
            continue

        for job in title_jobs:
            html += _job_card(job)

        html += '<div style="height:8px;"></div>\n'

    # ── Uncategorized (edge case) ─────────────────────────────────────────────
    uncategorized = [j for j in jobs if j.search_term not in job_titles]
    if uncategorized:
        html += (
            '<div style="font-size:17px;font-weight:700;color:#1e3a5f;'
            'padding:20px 20px 10px;border-bottom:2px solid #e8edf5;">Other Results</div>\n'
        )
        for job in sorted(uncategorized, key=_sort_key, reverse=True):
            html += _job_card(job)

    html += _HTML_FOOT.format(generated=now_str)
    return html


def build_plain_report(jobs: List[Job], config: dict) -> str:
    """Plain-text fallback for email clients that don't render HTML."""
    lines = [
        "JobHelp Version 1 — Daily Digest",
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
        f"Total results: {len(jobs)}",
        "=" * 60,
        "",
    ]
    by_title: dict[str, List[Job]] = defaultdict(list)
    for job in jobs:
        by_title[job.search_term].append(job)

    for title in config.get("job_titles", []):
        title_jobs = by_title.get(title, [])
        lines.append(f"\n{'─'*60}\n{title.upper()} ({len(title_jobs)} results)\n{'─'*60}")
        if not title_jobs:
            lines.append("  No results in the last 24 hours.\n")
            continue
        for job in title_jobs:
            lines.append(f"  {job.title}")
            lines.append(f"  {job.company} | {job.location} | {job.source}")
            if job.url:
                lines.append(f"  {job.url}")
            if job.posted:
                lines.append(f"  Posted: {_fmt_posted(job.posted)}")
            lines.append("")

    return "\n".join(lines)


# ── Sender ────────────────────────────────────────────────────────────────────

def send_report(jobs: List[Job], config: dict) -> bool:
    """
    Build and send the HTML email digest.
    Returns True on success.
    """
    email_cfg = config.get("email", {})
    recipient = email_cfg.get("recipient", "")
    sender = email_cfg.get("sender", "")
    password = email_cfg.get("password", "")
    smtp_server = email_cfg.get("smtp_server", "smtp.gmail.com")
    smtp_port = int(email_cfg.get("smtp_port", 587))
    use_tls = email_cfg.get("use_tls", True)
    sender_name = email_cfg.get("sender_name", "JobHelp Bot")
    hours = config.get("search", {}).get("hours_ago", 24)

    if not recipient:
        logger.error("No recipient email configured.")
        return False
    if not sender or not password:
        logger.error("Sender email / password not configured — cannot send.")
        return False

    subject = (
        f"JobHelp v1 — {len(jobs)} Tech Leadership Jobs "
        f"(last {hours}h) — {datetime.utcnow().strftime('%b %d, %Y')}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender}>"
    msg["To"] = recipient

    plain = build_plain_report(jobs, config)
    html = build_html_report(jobs, config)

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            if use_tls:
                server.starttls()
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        logger.info("Email sent to %s (%d jobs)", recipient, len(jobs))
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP authentication failed. For Gmail, use an App Password: "
            "https://myaccount.google.com/apppasswords"
        )
    except smtplib.SMTPException as exc:
        logger.error("SMTP error: %s", exc)
    except OSError as exc:
        logger.error("Network error sending email: %s", exc)

    return False
