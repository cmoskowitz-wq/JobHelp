# JobHelp Version 3

A configurable job board scraper that searches for tech leadership roles, emails
a rich HTML digest, scores jobs with Claude AI, and provides a local web
dashboard for tracking applications.

---

## What's New in v3

| Feature | Details |
|---------|---------|
| **Parallel scraping** | All boards run simultaneously via async Playwright — 3–5× faster |
| **LinkedIn fix** | Tries a lightweight guest API first; Playwright only as fallback with jitter — fixes "only works on first morning run" |
| **Salary display** | Extracted from titles, descriptions, and API responses; shown in email + dashboard |
| **Geo-priority** | NJ, CT, and NYC jobs float to the top of every section with a gold border |
| **AI scoring** | Claude scores each job 0–10 against your profile + writes a one-line match summary |
| **Fuzzy dedup** | rapidfuzz catches near-duplicate listings across boards (e.g. "VP Technology" ≈ "VP of Technology") |
| **Slack notifications** | Instant Slack message with top N jobs after each run |
| **SMS notifications** | Brief Twilio SMS with job count + top listings |
| **Web dashboard** | Browse jobs, filter by board/salary/score/geo, mark as Interested / Applied / Pass |
| **Application tracker** | SQLite-backed tracker with status, notes, and history |
| **New boards** | Builtin.com (tech-focused, strong NYC/NJ) and Wellfound (startup roles) |

---

## Supported Job Boards (14 total)

| Board | Method | Auth |
|-------|--------|------|
| Indeed | Playwright | None |
| LinkedIn | Guest API → Playwright fallback | None |
| Dice | Playwright | None |
| ZipRecruiter | Playwright | None |
| Glassdoor | Playwright | None |
| SimplyHired | Playwright | None |
| Monster | Playwright | None |
| CareerBuilder | Playwright | None |
| **Builtin** *(new)* | Playwright | None |
| **Wellfound** *(new)* | Playwright | None |
| The Muse | Public API | None |
| RemoteOK | Public API | None |
| Jobicy | Public API | None |
| **Adzuna** | Official API | Free key (optional) |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure credentials

```bash
cp .env.example .env   # if .env.example exists, otherwise create .env manually
```

Edit `.env`:

```
EMAIL_SENDER=you@gmail.com
EMAIL_PASSWORD=xxxx-xxxx-xxxx-xxxx   # Gmail App Password
ANTHROPIC_API_KEY=sk-ant-...          # optional — enables AI scoring
SLACK_WEBHOOK_URL=https://hooks.slack.com/...   # optional
```

> **Gmail App Password**: Create one at https://myaccount.google.com/apppasswords
> (requires 2-Step Verification)

### 3. Run

```bash
# Scrape all boards, email digest, then stay scheduled
python main.py

# One-shot run and exit
python main.py --now

# Dry run (print results, no email)
python main.py --dry-run

# Open web dashboard (http://localhost:5000)
python main.py --dashboard
```

---

## Configuration (`config.yaml`)

### Geo-Priority

Jobs in NJ, CT, and NYC are sorted to the **top of every section** and highlighted
with a gold border in the email and a 📍 badge on the dashboard. Regions are fully
configurable:

```yaml
geo_priority:
  regions:
    - "new jersey"
    - " nj"
    - "connecticut"
    - "new york city"
    - "nyc"
    - "manhattan"
    # add any city/state keywords here
```

### Salary

Salary is extracted automatically from job titles, descriptions, and API responses.
No configuration needed — it appears in the email card and dashboard whenever found.

### AI Scoring

Requires `ANTHROPIC_API_KEY` in your `.env`:

```yaml
ai:
  enabled: true
  model: "claude-haiku-4-5-20251001"   # fast and cheap
  min_score: 5                          # hide jobs scoring below 5
  profile: |
    Senior technology executive (CTO/CIO) with 15+ years...
    Prefer NJ/CT/NYC. Salary target $250K+.
```

### Slack Notifications

```yaml
notifications:
  slack:
    enabled: true
    webhook_url: "https://hooks.slack.com/services/..."
    max_jobs: 10
```

Or set `SLACK_WEBHOOK_URL` in your `.env` and leave `webhook_url` blank.

### SMS Notifications

```yaml
notifications:
  sms:
    enabled: true
    from_number: "+15551234567"
    to_number: "+19735550001"
```

Set `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` in `.env`.

---

## Web Dashboard

```bash
python main.py --dashboard          # launches on http://localhost:5000
python dashboard.py --port 8080    # custom port
```

Features:
- Browse today's jobs or pick a past date
- Filter by board, search term, salary, AI score, geo-priority
- Click any job title to open the original posting
- One-click: **Interested** / **Applied** / **Interviewing** / **Offer** / **Pass**
- **Applications tab** — full tracker with editable notes

---

## Running as a Background Service

### Linux (systemd)

```ini
[Unit]
Description=JobHelp Version 3
After=network.target

[Service]
User=youruser
WorkingDirectory=/path/to/JobHelp
EnvironmentFile=/path/to/JobHelp/.env
ExecStart=/usr/bin/python3 /path/to/JobHelp/main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable jobhelp && sudo systemctl start jobhelp
```

### cron (simple alternative)

```cron
0 8 * * * cd /path/to/JobHelp && python3 main.py --now >> jobhelp.log 2>&1
```

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt && playwright install chromium --with-deps
CMD ["python", "main.py"]
```

---

## Adding a New Job Board

1. Open `scrapers.py`
2. Create a class extending `BaseScraper`
3. Implement `async def fetch(self, job_title: str) -> List[Job]`
4. Register it in `SCRAPER_REGISTRY`
5. Add an entry to `config.yaml` under `job_boards`

---

## About the LinkedIn Fix

LinkedIn's bot detection resets roughly every 12–24 h per IP. Under the old
approach, the full Playwright page load was fingerprinted after the first daily
scrape, causing empty results on all subsequent runs.

**v3 fix (two-stage):**
1. Try `GET /jobs-guest/jobs/api/seeMoreJobPostings/search` — a lighter endpoint
   that returns HTML fragments with fewer bot-detection signals. This is a plain
   `requests` call with no browser.
2. If that returns 0 results, fall back to Playwright with a random 4–10 s jitter
   delay to vary the timing fingerprint.

This approach reliably returns results on repeated daily runs.

---

## Version History

| Version | Date | Notes |
|---------|------|-------|
| 1.0 | 2026-03-15 | Initial — 12 boards, HTML email, configurable schedule |
| 2.0 | 2026-03-16 | Playwright scraping, SQLite state, smart time-window |
| 3.0 | 2026-03-18 | Async parallel scraping, LinkedIn fix, salary, geo-priority, AI scoring, fuzzy dedup, Slack/SMS, web dashboard, application tracker, Builtin + Wellfound boards |
