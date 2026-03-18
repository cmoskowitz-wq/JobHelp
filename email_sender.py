"""
email_sender.py — JobHelp Version 3
Formats job results as an HTML email digest and sends via SMTP.

v3 changes:
  - Job cards now display salary when available
  - AI score badge + one-line AI summary in each card
  - Geo-priority jobs (NJ/CT/NYC) show a location pin badge and float to the
    top of each section
  - Subject line includes salary/geo highlights
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
<div style="max-width:620px;margin:0 auto;background:#f4f6f9;">
"""

_HTML_FOOT = """\
  <div style="background:#f8fafc;padding:16px 20px;font-size:12px;color:#aaa;
              border-top:1px solid #e2e8f0;text-align:center;">
    JobHelp v3 &bull; Generated {generated} UTC &bull;
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


def _fmt_salary(job: Job) -> str:
    """Return a human-readable salary string, or empty string."""
    if job.salary_text:
        return job.salary_text
    if job.salary_min:
        lo = f"${job.salary_min:,}"
        if job.salary_max and job.salary_max != job.salary_min:
            return f"{lo} – ${job.salary_max:,}"
        return lo
    return ""


def _escape(text: str) -> str:
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── Sort helpers ──────────────────────────────────────────────────────────────

def _sort_key(j: Job) -> tuple:
    """
    Primary:   geo_priority desc (True first)
    Secondary: ai_score desc
    Tertiary:  posted desc
    """
    geo = not j.geo_priority            # False < True, so negate to get True first
    score = -(j.ai_score if j.ai_score is not None else 5.0)
    posted = j.posted or datetime.min.replace(tzinfo=timezone.utc)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return (geo, score, -posted.timestamp())


# ── Report builder ────────────────────────────────────────────────────────────

def _job_card(job: Job) -> str:
    """Render a single job as an inline-styled card."""
    # ── Title (linked or plain) ───────────────────────────────────────────────
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

    # ── Badges ────────────────────────────────────────────────────────────────
    badges = ""
    if job.geo_priority:
        badges += (
            '<span style="background:#fef3c7;color:#92400e;font-size:10px;'
            'font-weight:700;padding:2px 7px;border-radius:4px;'
            'margin-left:8px;text-transform:uppercase;vertical-align:middle;">'
            '&#128205; NJ/CT/NYC</span>'
        )
    if job.remote:
        badges += (
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

    # ── AI score ──────────────────────────────────────────────────────────────
    ai_badge = ""
    if job.ai_score is not None:
        score_color = (
            "#166534" if job.ai_score >= 7
            else "#92400e" if job.ai_score >= 4
            else "#991b1b"
        )
        score_bg = (
            "#dcfce7" if job.ai_score >= 7
            else "#fef3c7" if job.ai_score >= 4
            else "#fee2e2"
        )
        ai_badge = (
            f'<span style="background:{score_bg};color:{score_color};font-size:11px;'
            f'font-weight:700;padding:2px 8px;border-radius:12px;margin-left:6px;">'
            f'AI&nbsp;{job.ai_score:.1f}/10</span>'
        )

    # ── Salary ────────────────────────────────────────────────────────────────
    salary_str = _fmt_salary(job)
    salary_html = ""
    if salary_str:
        salary_html = (
            f'<div style="color:#166534;font-size:13px;font-weight:600;margin-top:4px;">'
            f'&#128176; {_escape(salary_str)}</div>'
        )

    # ── AI summary ────────────────────────────────────────────────────────────
    summary_html = ""
    if job.ai_summary:
        summary_html = (
            f'<div style="color:#6b7280;font-size:12px;font-style:italic;margin-top:4px;">'
            f'{_escape(job.ai_summary)}</div>'
        )

    posted_str = _fmt_posted(job.posted)

    # ── Geo highlight: left border colour ─────────────────────────────────────
    border_color = "#f59e0b" if job.geo_priority else "#e2e8f0"

    return (
        f'<div style="background:#fff;border:1px solid {border_color};'
        f'border-left:4px solid {border_color};border-radius:8px;'
        f'margin:8px 16px;padding:14px 16px;">'
        f'  <div style="line-height:1.4;">{title_html}{badges}</div>'
        f'  <div style="color:#555;font-size:13px;margin-top:6px;">{_escape(job.company)}</div>'
        f'  <div style="color:#888;font-size:12px;margin-top:2px;">{_escape(job.location)}</div>'
        f'  {salary_html}'
        f'  {summary_html}'
        f'  <div style="margin-top:10px;">'
        f'    {board_badge}{ai_badge}'
        f'    <span style="color:#aaa;font-size:12px;margin-left:10px;">{posted_str}</span>'
        f'  </div>'
        f'</div>\n'
    )


def build_html_report(jobs: List[Job], config: dict) -> str:
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    hours = config.get("search", {}).get("hours_ago", 24)
    job_titles = config.get("job_titles", [])

    by_title: dict[str, List[Job]] = defaultdict(list)
    for job in jobs:
        by_title[job.search_term].append(job)

    geo_total = sum(1 for j in jobs if j.geo_priority)
    boards = len({j.source for j in jobs})
    ai_scored = sum(1 for j in jobs if j.ai_score is not None)

    html = _HTML_HEAD

    # ── Header ────────────────────────────────────────────────────────────────
    html += (
        f'<div style="background:linear-gradient(135deg,#1e3a5f 0%,#2563eb 100%);'
        f'color:#fff;padding:24px 20px;">'
        f'  <div style="font-size:22px;font-weight:700;margin:0 0 6px;">'
        f'    &#128188; JobHelp &mdash; Daily Digest'
        f'  </div>'
        f'  <div style="opacity:.85;font-size:13px;">'
        f'    Last {hours}h &bull; {now_str} UTC &bull; {len(jobs)} jobs found'
        f'  </div>'
        f'</div>\n'
    )

    # ── Summary bar ───────────────────────────────────────────────────────────
    geo_badge = (
        f'  <span style="margin-right:20px;">&#128205; NJ/CT/NYC: '
        f'<strong style="color:#92400e;">{geo_total}</strong></span>'
        if geo_total else ""
    )
    ai_badge_bar = (
        f'  <span>AI scored: <strong style="color:#1e3a5f;">{ai_scored}</strong></span>'
        if ai_scored else ""
    )
    html += (
        f'<div style="background:#f0f4ff;border-bottom:1px solid #dbe4f5;'
        f'padding:12px 20px;font-size:13px;color:#555;">'
        f'  <span style="margin-right:20px;">Total: <strong style="color:#1e3a5f;">{len(jobs)}</strong></span>'
        f'  <span style="margin-right:20px;">Boards: <strong style="color:#1e3a5f;">{boards}</strong></span>'
        f'  <span style="margin-right:20px;">Titles: <strong style="color:#1e3a5f;">{len(job_titles)}</strong></span>'
        f'  {geo_badge}{ai_badge_bar}'
        f'</div>\n'
    )

    # ── Section per job title ─────────────────────────────────────────────────
    for title in job_titles:
        title_jobs = sorted(by_title.get(title, []), key=_sort_key)
        count = len(title_jobs)
        geo_count = sum(1 for j in title_jobs if j.geo_priority)

        geo_note = (
            f'<span style="font-size:12px;color:#92400e;margin-left:8px;">'
            f'&#128205; {geo_count} local</span>'
            if geo_count else ""
        )

        html += (
            f'<div style="font-size:17px;font-weight:700;color:#1e3a5f;'
            f'padding:20px 20px 10px;border-bottom:2px solid #e8edf5;">'
            f'  {_escape(title)}'
            f'  <span style="font-size:13px;font-weight:400;color:#888;margin-left:8px;">'
            f'({count})</span>{geo_note}'
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

    # ── Uncategorized ─────────────────────────────────────────────────────────
    uncategorized = [j for j in jobs if j.search_term not in job_titles]
    if uncategorized:
        html += (
            '<div style="font-size:17px;font-weight:700;color:#1e3a5f;'
            'padding:20px 20px 10px;border-bottom:2px solid #e8edf5;">Other Results</div>\n'
        )
        for job in sorted(uncategorized, key=_sort_key):
            html += _job_card(job)

    html += _HTML_FOOT.format(generated=now_str)
    return html


def build_plain_report(jobs: List[Job], config: dict) -> str:
    lines = [
        "JobHelp Version 3 — Daily Digest",
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
        f"Total results: {len(jobs)}",
        "=" * 60,
        "",
    ]
    by_title: dict[str, List[Job]] = defaultdict(list)
    for job in jobs:
        by_title[job.search_term].append(job)

    for title in config.get("job_titles", []):
        title_jobs = sorted(by_title.get(title, []), key=_sort_key)
        lines.append(f"\n{'─'*60}\n{title.upper()} ({len(title_jobs)} results)\n{'─'*60}")
        if not title_jobs:
            lines.append("  No results in the last window.\n")
            continue
        for job in title_jobs:
            geo_flag = " [NJ/CT/NYC]" if job.geo_priority else ""
            lines.append(f"  {job.title}{geo_flag}")
            lines.append(f"  {job.company} | {job.location} | {job.source}")
            sal = _fmt_salary(job)
            if sal:
                lines.append(f"  Salary: {sal}")
            if job.ai_score is not None:
                lines.append(f"  AI Score: {job.ai_score:.1f}/10")
            if job.ai_summary:
                lines.append(f"  {job.ai_summary}")
            if job.url:
                lines.append(f"  {job.url}")
            if job.posted:
                lines.append(f"  Posted: {_fmt_posted(job.posted)}")
            lines.append("")

    return "\n".join(lines)


# ── Sender ────────────────────────────────────────────────────────────────────

def send_report(jobs: List[Job], config: dict) -> bool:
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

    geo_count = sum(1 for j in jobs if j.geo_priority)
    geo_note = f" | {geo_count} NJ/CT/NYC" if geo_count else ""
    subject = (
        f"JobHelp v3 — {len(jobs)} Tech Leadership Jobs "
        f"(last {hours}h{geo_note}) — {datetime.utcnow().strftime('%b %d, %Y')}"
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
