"""
ai_scorer.py — JobHelp Version 3
Uses the Anthropic Claude API to:
  1. Score each job 0–10 for relevance to the user's target profile.
  2. Generate a one-sentence summary explaining the match.

Requires:
  - ANTHROPIC_API_KEY environment variable
  - pip install anthropic

Config block (config.yaml):
  ai:
    enabled: true
    model: "claude-haiku-4-5-20251001"   # cheapest; swap for Sonnet if needed
    batch_size: 20                        # jobs per API call
    min_score: 0                          # filter out jobs scoring below this
    profile: |
      Experienced technology executive (CTO/CIO/VP of Technology) with 15+ years
      leading enterprise IT, infrastructure, and engineering teams.  Strong
      background in digital transformation, cloud strategy, and EUC.
      Prefer roles in NJ, CT, or NYC metro area.  Salary target: $250K+.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from scrapers import Job

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE = (
    "Experienced technology executive (CTO / CIO / VP of Technology) with 15+ years "
    "leading enterprise IT, infrastructure, and engineering teams. Background includes "
    "digital transformation, cloud strategy, end-user computing (EUC), and large-scale "
    "programme delivery. Prefer roles in NJ, CT, or the NYC metro area."
)

_SCORE_PROMPT = """\
You are a job-matching assistant for a senior technology executive.

## Candidate Profile
{profile}

## Jobs to Score
Score each job below from 0 to 10 for how well it matches the candidate profile.
Also write one concise sentence (≤ 20 words) explaining the match or mismatch.

Return ONLY a valid JSON array — no markdown, no extra text — in this exact format:
[
  {{"idx": 0, "score": 8.5, "summary": "Strong CTO match at a mid-size fintech in NJ."}},
  ...
]

## Job List
{job_list}
"""


def _build_job_list(jobs: List[Job]) -> str:
    lines = []
    for i, j in enumerate(jobs):
        sal = f" | Salary: {j.salary_text}" if j.salary_text else ""
        lines.append(
            f"{i}. [{j.source}] {j.title} @ {j.company} — {j.location}{sal}"
        )
    return "\n".join(lines)


def _call_claude(prompt: str, model: str) -> str | None:
    """Single Claude API call; returns raw text or None on failure."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as exc:
        logger.warning("[AI] Claude API call failed: %s", exc)
        return None


def _parse_scores(raw: str, batch: List[Job]) -> None:
    """Parse Claude JSON response and apply scores/summaries to jobs in-place."""
    try:
        # Strip any accidental markdown fences
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        items = json.loads(raw)
        for item in items:
            idx = int(item.get("idx", -1))
            if 0 <= idx < len(batch):
                batch[idx].ai_score = float(item.get("score", 0))
                batch[idx].ai_summary = str(item.get("summary", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("[AI] Failed to parse scoring response: %s", exc)


def score_jobs(jobs: List[Job], config: dict) -> List[Job]:
    """
    Score all jobs using Claude.  Jobs are processed in batches.
    Returns the same list with ai_score and ai_summary populated.
    Jobs without an AI score are assigned score=5 (neutral) so they still appear.
    If AI is not configured or the API key is missing, returns jobs unchanged.
    """
    ai_cfg = config.get("ai", {})
    if not ai_cfg.get("enabled", False):
        return jobs

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("[AI] ANTHROPIC_API_KEY not set — skipping scoring.")
        return jobs

    model = ai_cfg.get("model", "claude-haiku-4-5-20251001")
    batch_size = int(ai_cfg.get("batch_size", 20))
    profile = ai_cfg.get("profile", _DEFAULT_PROFILE)
    min_score = float(ai_cfg.get("min_score", 0))

    logger.info("[AI] Scoring %d job(s) with %s (batch=%d).", len(jobs), model, batch_size)

    for start in range(0, len(jobs), batch_size):
        batch = jobs[start: start + batch_size]
        job_list = _build_job_list(batch)
        prompt = _SCORE_PROMPT.format(profile=profile, job_list=job_list)
        raw = _call_claude(prompt, model)
        if raw:
            _parse_scores(raw, batch)
        else:
            logger.warning("[AI] No response for batch starting at index %d.", start)

    # Apply neutral score to any unscored jobs so they still show up
    for job in jobs:
        if job.ai_score is None:
            job.ai_score = 5.0

    # Optionally filter by minimum score
    if min_score > 0:
        before = len(jobs)
        jobs = [j for j in jobs if (j.ai_score or 0) >= min_score]
        logger.info("[AI] Filtered %d job(s) below score %.1f.", before - len(jobs), min_score)

    # Sort by score descending (within each search_term section email_sender re-groups)
    jobs.sort(key=lambda j: (j.search_term, -(j.ai_score or 0)))
    logger.info("[AI] Scoring complete.")
    return jobs
