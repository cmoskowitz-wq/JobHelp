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
<title>JobHelp Version 1 — Daily Digest</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                 Oxygen, Ubuntu, sans-serif;
    background: #f4f6f9;
    color: #333;
    margin: 0;
    padding: 0;
  }}
  .wrapper {{
    max-width: 780px;
    margin: 24px auto;
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,.08);
    overflow: hidden;
  }}
  .header {{
    background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%);
    color: #fff;
    padding: 28px 32px;
  }}
  .header h1 {{
    margin: 0 0 6px;
    font-size: 24px;
    font-weight: 700;
    letter-spacing: .3px;
  }}
  .header p {{
    margin: 0;
    opacity: .85;
    font-size: 14px;
  }}
  .summary-bar {{
    background: #f0f4ff;
    border-bottom: 1px solid #dbe4f5;
    padding: 12px 32px;
    font-size: 13px;
    color: #555;
    display: flex;
    gap: 24px;
  }}
  .summary-bar strong {{ color: #1e3a5f; font-size: 15px; }}
  .section-title {{
    font-size: 18px;
    font-weight: 700;
    color: #1e3a5f;
    padding: 24px 32px 8px;
    border-bottom: 2px solid #e8edf5;
    margin: 0;
  }}
  .board-badge {{
    display: inline-block;
    background: #e8edf5;
    color: #1e3a5f;
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 12px;
    margin-right: 6px;
    text-transform: uppercase;
    letter-spacing: .5px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }}
  th {{
    text-align: left;
    padding: 10px 16px;
    background: #f8fafc;
    color: #888;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .6px;
    font-weight: 600;
    border-bottom: 1px solid #e2e8f0;
  }}
  td {{
    padding: 12px 16px;
    border-bottom: 1px solid #f0f0f0;
    vertical-align: top;
  }}
  tr:hover td {{ background: #fafbff; }}
  a.job-link {{
    color: #2563eb;
    text-decoration: none;
    font-weight: 600;
  }}
  a.job-link:hover {{ text-decoration: underline; }}
  .company {{ color: #555; }}
  .location {{ color: #888; font-size: 13px; }}
  .posted {{ color: #aaa; font-size: 12px; white-space: nowrap; }}
  .remote-badge {{
    background: #dcfce7;
    color: #166534;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 4px;
    margin-left: 6px;
    text-transform: uppercase;
  }}
  .no-jobs {{
    padding: 20px 32px;
    color: #999;
    font-style: italic;
    font-size: 14px;
  }}
  .footer {{
    background: #f8fafc;
    padding: 18px 32px;
    font-size: 12px;
    color: #aaa;
    border-top: 1px solid #e2e8f0;
    text-align: center;
  }}
  .toc {{
    padding: 16px 32px 8px;
    font-size: 13px;
    color: #555;
  }}
  .toc a {{
    color: #2563eb;
    text-decoration: none;
    margin-right: 14px;
  }}
  .toc a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div class="wrapper">
"""

_HTML_FOOT = """\
  <div class="footer">
    JobHelp Version 1 &bull; Generated {generated} UTC<br>
    To adjust search titles, boards, or schedule — edit <code>config.yaml</code>.
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

def build_html_report(jobs: List[Job], config: dict) -> str:
    """Return a complete HTML email body."""
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    hours = config.get("search", {}).get("hours_ago", 24)

    # Group by search_term
    by_title: dict[str, List[Job]] = defaultdict(list)
    for job in jobs:
        by_title[job.search_term].append(job)

    # Build TOC entries
    job_titles = config.get("job_titles", [])
    toc_items = []
    for title in job_titles:
        count = len(by_title.get(title, []))
        anchor = title.lower().replace(" ", "-").replace("/", "")
        toc_items.append(
            f'<a href="#{anchor}">{_escape(title)} ({count})</a>'
        )

    # Header
    html = _HTML_HEAD
    html += f"""
  <div class="header">
    <h1>&#128188; JobHelp Version 1 &mdash; Daily Digest</h1>
    <p>Jobs posted in the last {hours} hours &bull; {now_str} UTC &bull; {len(jobs)} total results</p>
  </div>
  <div class="summary-bar">
    <span>Total jobs found: <strong>{len(jobs)}</strong></span>
    <span>Boards searched: <strong>{len({j.source for j in jobs})}</strong></span>
    <span>Titles searched: <strong>{len(job_titles)}</strong></span>
  </div>
  <div class="toc">
    <strong>Jump to:</strong>&nbsp; {''.join(toc_items)}
  </div>
"""

    # Section per job title
    for title in job_titles:
        title_jobs = by_title.get(title, [])
        anchor = title.lower().replace(" ", "-").replace("/", "")
        html += f'<h2 class="section-title" id="{anchor}">{_escape(title)}</h2>\n'

        if not title_jobs:
            html += '<p class="no-jobs">No results found in the last 24 hours for this title.</p>\n'
            continue

        html += """
  <table>
    <thead>
      <tr>
        <th>Job Title</th>
        <th>Company</th>
        <th>Location</th>
        <th>Board</th>
        <th>Posted</th>
      </tr>
    </thead>
    <tbody>
"""
        def _sort_key(j):
            if j.posted is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            if j.posted.tzinfo is None:
                return j.posted.replace(tzinfo=timezone.utc)
            return j.posted

        for job in sorted(title_jobs, key=_sort_key, reverse=True):
            link = (f'<a class="job-link" href="{_escape(job.url)}" '
                    f'target="_blank">{_escape(job.title)}</a>'
                    if job.url else _escape(job.title))
            remote_badge = '<span class="remote-badge">Remote</span>' if job.remote else ""
            html += f"""
      <tr>
        <td>{link}{remote_badge}</td>
        <td class="company">{_escape(job.company)}</td>
        <td class="location">{_escape(job.location)}</td>
        <td><span class="board-badge">{_escape(job.source)}</span></td>
        <td class="posted">{_fmt_posted(job.posted)}</td>
      </tr>"""

        html += "\n    </tbody>\n  </table>\n"

    # Any jobs whose title wasn't in the configured list (edge case)
    uncategorized = [j for j in jobs if j.search_term not in job_titles]
    if uncategorized:
        html += '<h2 class="section-title">Other Results</h2>\n'
        html += "<table><thead><tr><th>Title</th><th>Company</th><th>Location</th><th>Board</th><th>Posted</th></tr></thead><tbody>\n"
        for job in uncategorized:
            link = (f'<a class="job-link" href="{_escape(job.url)}">{_escape(job.title)}</a>'
                    if job.url else _escape(job.title))
            html += f"<tr><td>{link}</td><td>{_escape(job.company)}</td><td>{_escape(job.location)}</td><td><span class='board-badge'>{_escape(job.source)}</span></td><td class='posted'>{_fmt_posted(job.posted)}</td></tr>\n"
        html += "</tbody></table>\n"

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
