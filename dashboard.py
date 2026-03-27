"""
dashboard.py — JobHelp Version 3
Self-contained Flask web dashboard.

Launch:
  python dashboard.py                # default port 5000
  python main.py --dashboard         # launched from main

Features:
  - Browse today's scraped jobs (or pick a past date)
  - Filter by board, search term, geo-priority, salary, AI score
  - Click job title to open in new tab
  - One-click status updates: Interested / Applied / Pass
  - Full application tracker tab with notes editing
  - No external CDN — all styles inline
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date

from flask import Flask, jsonify, render_template_string, request

import state

logger = logging.getLogger(__name__)
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "jobhelp-v3-secret")

# ──────────────────────────────────────────────────────────────────────────────
# HTML template (single-file, no external dependencies)
# ──────────────────────────────────────────────────────────────────────────────

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JobHelp v3 Dashboard</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
         background: #f4f6f9; color: #333; font-size: 14px; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* Nav */
  nav { background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%);
        color: #fff; padding: 12px 24px; display: flex; align-items: center; gap: 24px; }
  nav h1 { font-size: 18px; font-weight: 700; }
  nav button { background: rgba(255,255,255,.15); color: #fff; border: 1px solid rgba(255,255,255,.3);
               padding: 5px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  nav button.active { background: #fff; color: #1e3a5f; }

  /* Layout */
  .container { max-width: 1100px; margin: 0 auto; padding: 20px 16px; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; align-items: center; }
  .toolbar select, .toolbar input { padding: 6px 10px; border: 1px solid #d1d5db;
                                     border-radius: 6px; font-size: 13px; background: #fff; }
  .toolbar label { font-size: 12px; color: #6b7280; display: flex; align-items: center; gap: 4px; }
  .count-badge { background: #1e3a5f; color: #fff; font-size: 11px; padding: 2px 8px;
                 border-radius: 10px; margin-left: 6px; }

  /* Job cards */
  .job-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
               margin-bottom: 10px; padding: 14px 16px; position: relative; }
  .job-card.geo { border-left: 4px solid #f59e0b; }
  .job-card .title { font-size: 15px; font-weight: 600; color: #2563eb; }
  .job-card .company { color: #555; font-size: 13px; margin-top: 3px; }
  .job-card .location { color: #888; font-size: 12px; }
  .job-card .meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px;
                    align-items: center; }
  .badge { font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 10px;
            text-transform: uppercase; letter-spacing: .4px; }
  .badge-board { background: #e8edf5; color: #1e3a5f; }
  .badge-geo   { background: #fef3c7; color: #92400e; }
  .badge-remote { background: #dcfce7; color: #166534; }
  .salary { color: #166534; font-size: 13px; font-weight: 600; margin-top: 4px; }
  .ai-summary { color: #6b7280; font-size: 12px; font-style: italic; margin-top: 3px; }
  .score-high { color: #166534; } .score-mid { color: #92400e; } .score-low { color: #991b1b; }

  /* Status buttons */
  .status-bar { display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }
  .status-bar button { font-size: 11px; padding: 3px 10px; border-radius: 5px;
                        border: 1px solid #d1d5db; cursor: pointer; background: #fff; }
  .status-bar button.active-interested { background: #dbeafe; border-color: #3b82f6; color: #1d4ed8; }
  .status-bar button.active-applied    { background: #d1fae5; border-color: #10b981; color: #065f46; }
  .status-bar button.active-pass       { background: #fee2e2; border-color: #ef4444; color: #991b1b; }
  .status-bar button.active-interviewing { background: #fef9c3; border-color: #eab308; color: #713f12; }
  .status-bar button.active-offer      { background: #f3e8ff; border-color: #a855f7; color: #581c87; }

  /* Applications tab */
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px;
           overflow: hidden; }
  th { background: #f8fafc; font-size: 12px; font-weight: 600; color: #6b7280;
       text-align: left; padding: 10px 12px; border-bottom: 1px solid #e5e7eb; }
  td { padding: 10px 12px; border-bottom: 1px solid #f3f4f6; font-size: 13px; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .notes-input { width: 100%; border: 1px solid #e5e7eb; border-radius: 4px;
                  padding: 4px 6px; font-size: 12px; resize: vertical; }
  .status-select { border: 1px solid #e5e7eb; border-radius: 4px; padding: 3px 6px;
                    font-size: 12px; }

  /* Misc */
  .empty { text-align: center; padding: 60px 20px; color: #9ca3af; font-size: 15px; }
  .spinner { display: none; color: #6b7280; font-size: 13px; padding: 20px; text-align: center; }
</style>
</head>
<body>

<nav>
  <h1>&#128188; JobHelp v3</h1>
  <button id="btn-jobs" class="active" onclick="showTab('jobs')">Jobs</button>
  <button id="btn-tracker" onclick="showTab('tracker')">&#128196; Applications</button>
  <span id="run-info" style="margin-left:auto;opacity:.7;font-size:12px;"></span>
</nav>

<!-- ── JOBS TAB ── -->
<div id="tab-jobs" class="container">
  <div class="toolbar">
    <label>Date:
      <select id="filter-date" onchange="loadJobs()"></select>
    </label>
    <label>Search term:
      <select id="filter-term" onchange="renderJobs()">
        <option value="">All titles</option>
      </select>
    </label>
    <label>Board:
      <select id="filter-board" onchange="renderJobs()">
        <option value="">All boards</option>
      </select>
    </label>
    <label>Min salary ($K):
      <input type="number" id="filter-salary" placeholder="0" style="width:80px"
             oninput="renderJobs()">
    </label>
    <label><input type="checkbox" id="filter-geo" onchange="renderJobs()"> NJ/CT/NYC only</label>
    <label><input type="checkbox" id="filter-all-runs" onchange="loadJobs()"> All runs today</label>
    <label>Min AI score:
      <select id="filter-score" onchange="renderJobs()">
        <option value="0">Any</option>
        <option value="6">6+</option>
        <option value="7">7+</option>
        <option value="8">8+</option>
      </select>
    </label>
    <span>Showing <strong id="showing-count">0</strong> jobs<span id="geo-total"></span></span>
  </div>
  <div id="jobs-list"><div class="spinner">Loading…</div></div>
</div>

<!-- ── TRACKER TAB ── -->
<div id="tab-tracker" class="container" style="display:none">
  <div class="toolbar">
    <label>Status:
      <select id="tracker-status-filter" onchange="loadTracker()">
        <option value="">All</option>
        <option value="interested">Interested</option>
        <option value="applied">Applied</option>
        <option value="interviewing">Interviewing</option>
        <option value="offer">Offer</option>
        <option value="rejected">Rejected</option>
        <option value="new">New</option>
      </select>
    </label>
  </div>
  <div id="tracker-content"></div>
</div>

<script>
let allJobs = [];
let currentTab = 'jobs';

function showTab(tab) {
  currentTab = tab;
  document.getElementById('tab-jobs').style.display = tab === 'jobs' ? '' : 'none';
  document.getElementById('tab-tracker').style.display = tab === 'tracker' ? '' : 'none';
  document.getElementById('btn-jobs').className = tab === 'jobs' ? 'active' : '';
  document.getElementById('btn-tracker').className = tab === 'tracker' ? 'active' : '';
  if (tab === 'tracker') loadTracker();
}

// ── Date picker ──────────────────────────────────────────────────────────────
async function loadDates() {
  const resp = await fetch('/api/dates');
  const dates = await resp.json();
  const sel = document.getElementById('filter-date');
  sel.innerHTML = dates.map(d => `<option value="${d}">${d}</option>`).join('');
  if (dates.length) loadJobs();
}

// ── Jobs ─────────────────────────────────────────────────────────────────────
async function loadJobs() {
  const date = document.getElementById('filter-date').value;
  const allRuns = document.getElementById('filter-all-runs').checked;
  const resp = await fetch(`/api/jobs?date=${date}&all_runs=${allRuns}`);
  allJobs = await resp.json();

  // populate filter dropdowns
  const terms = [...new Set(allJobs.map(j => j.search_term).filter(Boolean))].sort();
  const boards = [...new Set(allJobs.map(j => j.source).filter(Boolean))].sort();
  const termSel = document.getElementById('filter-term');
  const boardSel = document.getElementById('filter-board');
  const curTerm = termSel.value;
  const curBoard = boardSel.value;
  termSel.innerHTML = '<option value="">All titles</option>' +
    terms.map(t => `<option value="${t}">${t}</option>`).join('');
  boardSel.innerHTML = '<option value="">All boards</option>' +
    boards.map(b => `<option value="${b}">${b}</option>`).join('');
  if (curTerm) termSel.value = curTerm;
  if (curBoard) boardSel.value = curBoard;

  const info = document.getElementById('run-info');
  info.textContent = `${allJobs.length} jobs cached for ${date || 'today'}`;
  renderJobs();
}

function renderJobs() {
  const term = document.getElementById('filter-term').value;
  const board = document.getElementById('filter-board').value;
  const geoOnly = document.getElementById('filter-geo').checked;
  const minSal = parseFloat(document.getElementById('filter-salary').value || 0) * 1000;
  const minScore = parseFloat(document.getElementById('filter-score').value || 0);

  let jobs = allJobs.filter(j => {
    if (term && j.search_term !== term) return false;
    if (board && j.source !== board) return false;
    if (geoOnly && !j.geo_priority) return false;
    if (minSal && (!j.salary_min || j.salary_min < minSal)) return false;
    if (minScore && (j.ai_score == null || j.ai_score < minScore)) return false;
    return true;
  });

  document.getElementById('showing-count').textContent = jobs.length;
  const geoCount = jobs.filter(j => j.geo_priority).length;
  document.getElementById('geo-total').textContent =
    geoCount ? ` (${geoCount} NJ/CT/NYC)` : '';

  const container = document.getElementById('jobs-list');
  if (!jobs.length) {
    container.innerHTML = '<div class="empty">No jobs match the current filters.</div>';
    return;
  }
  container.innerHTML = jobs.map(jobCard).join('');
}

function jobCard(j) {
  const geo = j.geo_priority ? ' geo' : '';
  const title = j.url
    ? `<a class="title" href="${esc(j.url)}" target="_blank">${esc(j.title)}</a>`
    : `<span class="title">${esc(j.title)}</span>`;
  const geoBadge = j.geo_priority
    ? '<span class="badge badge-geo">&#128205; NJ/CT/NYC</span>' : '';
  const remoteBadge = j.remote
    ? '<span class="badge badge-remote">Remote</span>' : '';
  const salary = j.salary_text
    ? `<div class="salary">&#128176; ${esc(j.salary_text)}</div>`
    : (j.salary_min ? `<div class="salary">&#128176; $${j.salary_min.toLocaleString()}` +
        (j.salary_max && j.salary_max !== j.salary_min ? ` – $${j.salary_max.toLocaleString()}` : '') +
        '</div>' : '');
  const scoreClass = j.ai_score >= 7 ? 'score-high' : j.ai_score >= 4 ? 'score-mid' : 'score-low';
  const scoreBadge = j.ai_score != null
    ? `<span class="badge ${scoreClass}" style="background:none;border:1px solid currentColor;">` +
      `AI ${j.ai_score.toFixed(1)}</span>` : '';
  const summary = j.ai_summary
    ? `<div class="ai-summary">${esc(j.ai_summary)}</div>` : '';
  const posted = j.posted ? `<span style="color:#9ca3af;font-size:11px;">${j.posted.substring(0,10)}</span>` : '';

  // retrieve saved status
  const savedStatus = window._appStatus[j.job_key] || '';
  const statusButtons = ['interested','applied','interviewing','offer','rejected'].map(s => {
    const active = savedStatus === s ? `active-${s}` : '';
    return `<button class="${active}" onclick="setStatus('${j.job_key}','${s}',this)">${cap(s)}</button>`;
  }).join('');

  return `<div class="job-card${geo}" id="card-${j.job_key}">
    ${title}
    <div class="company">${esc(j.company)}</div>
    <div class="location">${esc(j.location)}</div>
    ${salary}${summary}
    <div class="meta">
      <span class="badge badge-board">${esc(j.source)}</span>
      ${geoBadge}${remoteBadge}${scoreBadge}${posted}
    </div>
    <div class="status-bar">${statusButtons}</div>
  </div>`;
}

window._appStatus = {};

async function loadAppStatuses() {
  const resp = await fetch('/api/applications');
  const apps = await resp.json();
  window._appStatus = {};
  apps.forEach(a => { window._appStatus[a.job_key] = a.status; });
}

async function setStatus(jobKey, status, btn) {
  const card = document.getElementById('card-' + jobKey);
  const allBtns = card.querySelectorAll('.status-bar button');
  allBtns.forEach(b => b.className = '');
  // toggle off if already active
  const current = window._appStatus[jobKey];
  if (current === status) {
    delete window._appStatus[jobKey];
    await fetch(`/api/job/${jobKey}/status`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({status: 'new', notes: ''})
    });
    return;
  }
  btn.className = 'active-' + status;
  window._appStatus[jobKey] = status;
  await fetch(`/api/job/${jobKey}/status`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({status, notes: ''})
  });
}

// ── Tracker ──────────────────────────────────────────────────────────────────
async function loadTracker() {
  const status = document.getElementById('tracker-status-filter').value;
  const url = '/api/applications' + (status ? `?status=${status}` : '');
  const resp = await fetch(url);
  const apps = await resp.json();
  const container = document.getElementById('tracker-content');
  if (!apps.length) {
    container.innerHTML = '<div class="empty">No applications tracked yet.<br>Use the Jobs tab to mark listings.</div>';
    return;
  }
  const rows = apps.map(a => `
    <tr>
      <td>${a.url ? `<a href="${esc(a.url)}" target="_blank">${esc(a.title||'')}</a>` : esc(a.title||'')}</td>
      <td>${esc(a.company||'')}</td>
      <td>${esc(a.location||'')}</td>
      <td>
        <select class="status-select" onchange="updateTrackerStatus('${a.job_key}', this.value)">
          ${['new','interested','applied','interviewing','offer','rejected'].map(s =>
            `<option value="${s}"${a.status===s?' selected':''}>${cap(s)}</option>`).join('')}
        </select>
      </td>
      <td>
        <textarea class="notes-input" rows="2"
          onblur="saveNotes('${a.job_key}', this.value)">${esc(a.notes||'')}</textarea>
      </td>
      <td style="color:#9ca3af;font-size:11px;">${(a.updated_ts||'').substring(0,10)}</td>
    </tr>`).join('');
  container.innerHTML = `<table>
    <thead><tr>
      <th>Title</th><th>Company</th><th>Location</th><th>Status</th><th>Notes</th><th>Updated</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function updateTrackerStatus(jobKey, status) {
  const notesEl = document.querySelector(`[onblur*="${jobKey}"]`);
  const notes = notesEl ? notesEl.value : '';
  await fetch(`/api/job/${jobKey}/status`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({status, notes})
  });
}

async function saveNotes(jobKey, notes) {
  const statusEl = document.querySelector(`[onchange*="${jobKey}"]`);
  const status = statusEl ? statusEl.value : 'new';
  await fetch(`/api/job/${jobKey}/status`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({status, notes})
  });
}

// ── Utilities ────────────────────────────────────────────────────────────────
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
                  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

// ── Init ─────────────────────────────────────────────────────────────────────
(async () => {
  await loadAppStatuses();
  await loadDates();
})();
</script>
</body>
</html>
"""


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(_TEMPLATE)


@app.route("/api/dates")
def api_dates():
    return jsonify(state.get_available_dates())


@app.route("/api/jobs")
def api_jobs():
    run_date = request.args.get("date") or date.today().isoformat()
    all_runs = request.args.get("all_runs", "false").lower() == "true"
    jobs = state.get_cached_jobs(run_date, latest_run_only=not all_runs)
    return jsonify(jobs)


@app.route("/api/job/<job_key>/status", methods=["POST"])
def api_update_status(job_key: str):
    data = request.get_json(silent=True) or {}
    status = data.get("status", "new")
    notes = data.get("notes", "")
    try:
        state.update_application(job_key, status, notes)
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/applications")
def api_applications():
    status_filter = request.args.get("status") or None
    return jsonify(state.get_applications(status_filter))


# ── Entry point ───────────────────────────────────────────────────────────────

def run_dashboard(host: str = "0.0.0.0", port: int = 5000, debug: bool = False) -> None:
    logger.info("Starting JobHelp dashboard at http://localhost:%d", port)
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="JobHelp v3 Web Dashboard")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    run_dashboard(host=args.host, port=args.port, debug=args.debug)
