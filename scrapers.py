"""
scrapers.py — JobHelp Version 3
Async Playwright scrapers for HTML-based boards + requests-based scrapers
for public API boards.

Key v3 changes:
  - Full async/await via playwright.async_api (all boards run in parallel)
  - LinkedIn fix: tries lightweight guest fragment API first; falls back to
    Playwright only when that returns nothing.  Per-board cooldown in state.py
    prevents hammering LinkedIn on repeat runs.
  - Salary extraction from titles, descriptions, and API fields
  - Two new boards: Builtin and Wellfound (AngelList Talent)
  - Fuzzy cross-board deduplication (rapidfuzz token_sort_ratio ≥ 88)
  - geo_sort() helper: NJ / CT / NYC jobs float to the top of each section
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/html, */*",
}

REQUEST_TIMEOUT = 20

# Geo-priority keywords (checked case-insensitively against location strings)
_GEO_PRIORITY_DEFAULTS = [
    "new jersey", " nj", ", nj", "(nj)",
    "connecticut", " ct", ", ct", "(ct)",
    "new york city", "nyc", "new york, ny", " ny,",
    "manhattan", "brooklyn", "queens", "bronx", "staten island",
    "jersey city", "hoboken", "newark", "stamford", "hartford",
    "bridgeport", "new haven",
]


# ── Job dataclass ─────────────────────────────────────────────────────────────

@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    source: str
    posted: Optional[datetime] = None
    description: str = ""
    search_term: str = ""
    remote: bool = False
    tags: List[str] = field(default_factory=list)
    # v3 additions
    salary_text: str = ""          # raw string as scraped
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    ai_score: Optional[float] = None   # 0–10, filled by ai_scorer
    ai_summary: str = ""               # one-sentence Claude summary
    geo_priority: bool = False         # True → job is in NJ/CT/NYC

    def is_recent(self, hours: int = 24) -> bool:
        if self.posted is None:
            return True
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        posted = self.posted
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return posted >= cutoff

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "url": self.url,
            "source": self.source,
            "posted": self.posted.isoformat() if self.posted else None,
            "description": self.description,
            "search_term": self.search_term,
            "remote": self.remote,
            "salary_text": self.salary_text,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "ai_score": self.ai_score,
            "ai_summary": self.ai_summary,
            "geo_priority": self.geo_priority,
        }


# ── Salary helpers ─────────────────────────────────────────────────────────────

_SALARY_PATTERNS = [
    # $150,000 - $200,000 / $150K - $200K
    re.compile(r'\$\s*([\d,]+)[Kk]?\s*[-–—]\s*\$\s*([\d,]+)[Kk]?', re.I),
    # $150K
    re.compile(r'\$\s*([\d,]+)\s*[Kk]\b', re.I),
    # 150000 - 200000 (plain numbers near "salary" or "compensation")
    re.compile(r'(?:salary|compensation|pay)[^\d]{0,20}([\d,]{5,7})\s*[-–—]\s*([\d,]{5,7})', re.I),
]


def _parse_salary_number(raw: str) -> int:
    """Turn '150,000' or '150K' or '150' into an integer."""
    raw = raw.replace(",", "").strip()
    val = float(raw)
    if val < 1000:          # treat small values as thousands (e.g. "150" → $150k)
        val *= 1000
    return int(val)


def extract_salary(text: str) -> tuple[str, Optional[int], Optional[int]]:
    """
    Returns (salary_text, salary_min, salary_max).
    salary_text is the raw matched substring; min/max are parsed integers or None.
    """
    if not text:
        return "", None, None
    for pat in _SALARY_PATTERNS:
        m = pat.search(text)
        if m:
            raw = m.group(0).strip()
            groups = [g for g in m.groups() if g]
            try:
                if len(groups) >= 2:
                    lo = _parse_salary_number(groups[0])
                    hi = _parse_salary_number(groups[1])
                    return raw, min(lo, hi), max(lo, hi)
                elif len(groups) == 1:
                    val = _parse_salary_number(groups[0])
                    return raw, val, val
            except (ValueError, AttributeError):
                return raw, None, None
    return "", None, None


# ── Geo helpers ────────────────────────────────────────────────────────────────

def _is_geo_priority(location: str, keywords: list[str]) -> bool:
    loc = location.lower()
    return any(kw in loc for kw in keywords)


def geo_sort(jobs: List[Job], geo_keywords: list[str] | None = None) -> List[Job]:
    """
    Sort jobs so that geo-priority ones (NJ/CT/NYC by default) appear first.
    Preserves relative order within each group.
    """
    kws = [k.lower() for k in (geo_keywords or _GEO_PRIORITY_DEFAULTS)]
    for job in jobs:
        job.geo_priority = _is_geo_priority(job.location, kws)
    # stable sort: geo_priority=True first
    return sorted(jobs, key=lambda j: (not j.geo_priority,))


# ── Base scraper ───────────────────────────────────────────────────────────────

class BaseScraper:
    name: str = "base"

    def __init__(self, config: dict, browser=None):
        self.config = config
        self.browser = browser
        search_cfg = config.get("search", {})
        self.hours_ago: int = search_cfg.get("hours_ago", 24)
        self.location: str = search_cfg.get("location", "")
        self.max_results: int = search_cfg.get("results_per_board", 25)

    # ── Async Playwright fetch ─────────────────────────────────────────────────

    async def _pw_get(
        self,
        url: str,
        wait_selector: str | None = None,
        wait_ms: int = 2500,
    ) -> BeautifulSoup | None:
        """Navigate to *url* with async Playwright and return parsed HTML."""
        if self.browser is None:
            logger.warning("[%s] No browser instance available.", self.name)
            return None
        ctx = None
        try:
            ctx = await self.browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = await ctx.new_page()
            await page.route(
                "**/{ads,analytics,doubleclick,googlesyndication}**",
                lambda route, _: route.abort(),
            )
            await page.goto(url, timeout=35_000, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=8_000)
                except Exception:
                    pass
            else:
                await page.wait_for_timeout(wait_ms)
            html = await page.content()
            return BeautifulSoup(html, "lxml")
        except Exception as exc:
            logger.warning("[%s] Playwright error on %s: %s", self.name, url, exc)
            return None
        finally:
            if ctx:
                await ctx.close()

    # ── Sync requests fetch (run in thread) ───────────────────────────────────

    def _api_get_sync(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        json_response: bool = False,
    ):
        """Blocking requests wrapper with one retry — call via _api_get()."""
        h = {**_API_HEADERS, **(headers or {})}
        for attempt in range(2):
            try:
                resp = requests.get(
                    url, params=params, headers=h, timeout=REQUEST_TIMEOUT
                )
                resp.raise_for_status()
                return resp.json() if json_response else resp
            except requests.RequestException as exc:
                if attempt == 0:
                    time.sleep(2)
                else:
                    logger.warning("[%s] Request failed for %s: %s", self.name, url, exc)
        return None

    async def _api_get(self, url, params=None, headers=None, json_response=False):
        """Non-blocking wrapper: runs _api_get_sync in a thread."""
        return await asyncio.to_thread(
            self._api_get_sync, url, params, headers, json_response
        )

    # ── Search loop ───────────────────────────────────────────────────────────

    async def fetch(self, job_title: str) -> List[Job]:
        raise NotImplementedError

    async def search_all(self, job_titles: List[str]) -> List[Job]:
        results: List[Job] = []
        for title in job_titles:
            try:
                jobs = await self.fetch(title)
                for j in jobs:
                    j.search_term = title
                results.extend(jobs)
                await asyncio.sleep(1.5)
            except Exception as exc:
                logger.error("[%s] Error searching '%s': %s", self.name, title, exc)

        # If the board returned nothing across all titles, retry the first title
        # once with a longer wait — catches slow-loading Playwright pages.
        if not results and job_titles:
            logger.info("[%s] Zero results — retrying '%s' with extended wait.", self.name, job_titles[0])
            await asyncio.sleep(4)
            try:
                retry_jobs = await self.fetch(job_titles[0])
                for j in retry_jobs:
                    j.search_term = job_titles[0]
                results.extend(retry_jobs)
                if retry_jobs:
                    logger.info("[%s] Retry recovered %d result(s).", self.name, len(retry_jobs))
            except Exception as exc:
                logger.warning("[%s] Retry also failed: %s", self.name, exc)

        return results


# ── Indeed ────────────────────────────────────────────────────────────────────

class IndeedScraper(BaseScraper):
    name = "Indeed"

    async def fetch(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "q": job_title, "sort": "date",
            "fromage": "1", "l": self.location or "",
        })
        url = f"https://www.indeed.com/jobs?{params}"
        soup = await self._pw_get(
            url, wait_selector="[data-testid='jobsearch-ResultsList']"
        )
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select("div.job_seen_beacon, li[data-testid='job-result']")
        for card in cards[: self.max_results]:
            title_el = (
                card.select_one("h2.jobTitle a span[title]")
                or card.select_one("h2.jobTitle a")
                or card.select_one("[data-testid='jobTitle']")
            )
            company_el = (
                card.select_one("[data-testid='company-name']")
                or card.select_one("span.companyName")
            )
            location_el = (
                card.select_one("[data-testid='text-location']")
                or card.select_one("div.companyLocation")
            )
            link_el = card.select_one("h2.jobTitle a[data-jk], h2.jobTitle a[id]")
            salary_el = card.select_one(
                "[data-testid='attribute_snippet_testid'], "
                "div.metadata.salary-snippet-container, "
                "div[class*='salary']"
            )
            date_el = card.select_one("[data-testid='myJobsStateDate'], span.date")

            if not title_el:
                continue

            href = ""
            if link_el:
                jk = link_el.get("data-jk", "")
                href = f"https://www.indeed.com/viewjob?jk={jk}" if jk else link_el.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.indeed.com" + href

            posted = _parse_relative_date(date_el.get_text(strip=True) if date_el else "")
            raw_salary = salary_el.get_text(strip=True) if salary_el else ""
            sal_text, sal_min, sal_max = extract_salary(raw_salary)

            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href, source=self.name, posted=posted,
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            )
            if job.is_recent(self.hours_ago):
                jobs.append(job)
        return jobs


# ── LinkedIn ──────────────────────────────────────────────────────────────────

class LinkedInScraper(BaseScraper):
    """
    v3 LinkedIn fix: tries the lightweight guest-fragment API first (plain
    requests, no browser fingerprint).  Only falls back to Playwright when
    the fragment API returns nothing — and then adds per-run jitter so the
    same fingerprint isn't seen on every subsequent run.

    Why the old approach broke on run 2+:
      LinkedIn's bot-detection resets roughly every 12–24 h per IP.  After
      the first Playwright hit each day, it flags the session and starts
      returning empty results or redirect loops.  The guest fragment endpoint
      is a lighter JSON-like endpoint that shares fewer signals.
    """
    name = "LinkedIn"
    _GUEST_API = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    )

    async def fetch(self, job_title: str) -> List[Job]:
        # ── Strategy 1: guest fragment API (requests, no browser) ─────────
        jobs = await asyncio.to_thread(self._fetch_guest_api, job_title)
        if jobs:
            logger.info("[LinkedIn] Guest API returned %d result(s) for '%s'.", len(jobs), job_title)
            return jobs

        # ── Strategy 2: Playwright fallback with extended jitter ───────────
        logger.info("[LinkedIn] Guest API empty for '%s', trying Playwright.", job_title)
        import random
        await asyncio.sleep(random.uniform(4, 10))
        return await self._fetch_playwright(job_title)

    def _fetch_guest_api(self, job_title: str) -> List[Job]:
        """Synchronous; called via asyncio.to_thread."""
        params = {
            "keywords": job_title,
            "location": self.location or "United States",
            "f_TPR": "r86400",
            "start": "0",
        }
        headers = {
            **_API_HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.linkedin.com/jobs/search/",
        }
        resp = self._api_get_sync(self._GUEST_API, params=params, headers=headers)
        if resp is None:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        jobs: List[Job] = []

        cards = soup.select(
            "li.jobs-search-results__list-item, "
            "div.base-card, li.job-search-card"
        )
        for card in cards[: self.max_results]:
            title_el = (
                card.select_one("h3.base-search-card__title")
                or card.select_one("h3.job-search-card__title")
            )
            company_el = (
                card.select_one("h4.base-search-card__subtitle a")
                or card.select_one("h4.base-search-card__subtitle")
            )
            location_el = card.select_one(
                "span.job-search-card__location, "
                "span.base-search-card__metadata"
            )
            link_el = card.select_one(
                "a.base-card__full-link, a.job-search-card__title-link"
            )
            time_el = card.select_one("time[datetime]")

            if not title_el or not link_el:
                continue

            posted = None
            if time_el and time_el.get("datetime"):
                posted = _parse_iso(time_el["datetime"])

            salary_el = card.select_one(
                "span.job-search-card__salary-info, "
                "[class*='salary']"
            )
            raw_salary = salary_el.get_text(strip=True) if salary_el else ""
            sal_text, sal_min, sal_max = extract_salary(raw_salary)

            href = link_el.get("href", "").split("?")[0]
            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href, source=self.name, posted=posted,
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            )
            # Always use 24h window for LinkedIn — the state dedup layer prevents
            # re-sending jobs from earlier runs, so is_recent(hours_ago) would
            # incorrectly drop all jobs on subsequent (shorter window) runs.
            if job.is_recent(24):
                jobs.append(job)
        return jobs

    async def _fetch_playwright(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "keywords": job_title,
            "location": self.location or "United States",
            "f_TPR": "r86400",
            "position": 1, "pageNum": 0,
        })
        url = f"https://www.linkedin.com/jobs/search?{params}"
        soup = await self._pw_get(
            url,
            wait_selector="ul.jobs-search__results-list, div.jobs-search-results-grid",
            wait_ms=4000,
        )
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select(
            "li.jobs-search-results__list-item, "
            "div.base-card, li.job-search-card"
        )
        for card in cards[: self.max_results]:
            title_el = (
                card.select_one("h3.base-search-card__title")
                or card.select_one("h3.job-search-card__title")
            )
            company_el = (
                card.select_one("h4.base-search-card__subtitle a")
                or card.select_one("h4.base-search-card__subtitle")
            )
            location_el = card.select_one(
                "span.job-search-card__location, "
                "span.base-search-card__metadata"
            )
            link_el = card.select_one(
                "a.base-card__full-link, a.job-search-card__title-link"
            )
            time_el = card.select_one("time[datetime]")
            salary_el = card.select_one("[class*='salary']")

            if not title_el or not link_el:
                continue

            posted = None
            if time_el and time_el.get("datetime"):
                posted = _parse_iso(time_el["datetime"])

            raw_salary = salary_el.get_text(strip=True) if salary_el else ""
            sal_text, sal_min, sal_max = extract_salary(raw_salary)

            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=link_el["href"].split("?")[0], source=self.name, posted=posted,
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            )
            if job.is_recent(24):  # always 24h — dedup prevents re-sends
                jobs.append(job)
        return jobs


# ── ZipRecruiter ──────────────────────────────────────────────────────────────

class ZipRecruiterScraper(BaseScraper):
    name = "ZipRecruiter"

    async def fetch(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "search": job_title, "location": self.location or "", "days": "1",
        })
        url = f"https://www.ziprecruiter.com/candidate/search?{params}"
        soup = await self._pw_get(
            url, wait_selector="article.job_result, div[data-testid='job-card']"
        )
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select(
            "article.job_result, div[data-testid='job-card']"
        )[: self.max_results]
        for card in cards:
            title_el = (
                card.select_one("h2.title a")
                or card.select_one("[data-testid='job-title']")
                or card.select_one("a.job_link")
            )
            company_el = (
                card.select_one("a.company_name")
                or card.select_one("[data-testid='company-name']")
            )
            location_el = (
                card.select_one("a.location")
                or card.select_one("[data-testid='location']")
            )
            salary_el = card.select_one(
                "span.salary_range, [data-testid='salary'], [class*='salary']"
            )

            if not title_el:
                continue

            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.ziprecruiter.com" + href

            raw_salary = salary_el.get_text(strip=True) if salary_el else ""
            sal_text, sal_min, sal_max = extract_salary(raw_salary)

            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href, source=self.name,
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            ))
        return jobs


# ── Glassdoor ─────────────────────────────────────────────────────────────────

class GlassdoorScraper(BaseScraper):
    name = "Glassdoor"

    async def fetch(self, job_title: str) -> List[Job]:
        encoded = urllib.parse.quote_plus(job_title)
        url = (
            f"https://www.glassdoor.com/Job/jobs.htm"
            f"?sc.keyword={encoded}&fromAge=1&sort.sortType=date&sort.descending=true"
        )
        soup = await self._pw_get(
            url, wait_selector="li[data-test='jobListing'], li.react-job-listing",
            wait_ms=3000,
        )
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select(
            "li[data-test='jobListing'], li.react-job-listing"
        )[: self.max_results]
        for card in cards:
            title_el = (
                card.select_one("[data-test='job-title']")
                or card.select_one("a.jobLink")
            )
            company_el = card.select_one("[data-test='employer-name']")
            location_el = card.select_one("[data-test='emp-location']")
            link_el = card.select_one(
                "a[href*='/job-listing/'], a[href*='/partner/jobListing']"
            )
            salary_el = card.select_one("[data-test='detailSalary'], [class*='salary']")

            if not title_el:
                continue

            href = ""
            if link_el:
                href = link_el.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.glassdoor.com" + href

            raw_salary = salary_el.get_text(strip=True) if salary_el else ""
            sal_text, sal_min, sal_max = extract_salary(raw_salary)

            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href, source=self.name,
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            ))
        return jobs


# ── SimplyHired ───────────────────────────────────────────────────────────────

class SimplyHiredScraper(BaseScraper):
    name = "SimplyHired"

    async def fetch(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "q": job_title, "l": self.location or "", "dateposted": "1",
        })
        url = f"https://www.simplyhired.com/search?{params}"
        soup = await self._pw_get(
            url, wait_selector="div[data-testid='job-card'], article.SerpJob"
        )
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select(
            "div[data-testid='job-card'], article.SerpJob"
        )[: self.max_results]
        for card in cards:
            title_el = (
                card.select_one("[data-testid='jobTitle']")
                or card.select_one("h3.jobposting-title a")
            )
            company_el = (
                card.select_one("[data-testid='company']")
                or card.select_one("span.jobposting-company")
            )
            location_el = (
                card.select_one("[data-testid='searchSerpJobLocation']")
                or card.select_one("span.jobposting-location")
            )
            link_el = (
                card.select_one("a[data-testid='job-title-link']")
                or card.select_one("a.jobposting-permalink")
            )
            salary_el = card.select_one(
                "[data-testid='salary'], span.jobposting-salary, [class*='salary']"
            )

            if not title_el:
                continue

            href = ""
            if link_el:
                href = link_el.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.simplyhired.com" + href

            raw_salary = salary_el.get_text(strip=True) if salary_el else ""
            sal_text, sal_min, sal_max = extract_salary(raw_salary)

            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href, source=self.name,
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            ))
        return jobs


# ── Monster ───────────────────────────────────────────────────────────────────

class MonsterScraper(BaseScraper):
    name = "Monster"

    async def fetch(self, job_title: str) -> List[Job]:
        encoded = urllib.parse.quote_plus(job_title)
        loc = urllib.parse.quote_plus(self.location or "")
        url = f"https://www.monster.com/jobs/search?q={encoded}&where={loc}&tm=1"
        soup = await self._pw_get(
            url, wait_selector="div[data-testid='JobCard'], section.card-content"
        )
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select(
            "div[data-testid='JobCard'], section.card-content"
        )[: self.max_results]
        for card in cards:
            title_el = (
                card.select_one("[data-testid='jobTitle']")
                or card.select_one("h2.title")
            )
            company_el = (
                card.select_one("[data-testid='company']")
                or card.select_one("div.company")
            )
            location_el = (
                card.select_one("[data-testid='location']")
                or card.select_one("div.location")
            )
            link_el = card.select_one("a")

            if not title_el or not link_el:
                continue

            href = link_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.monster.com" + href

            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href, source=self.name,
            ))
        return jobs


# ── CareerBuilder ─────────────────────────────────────────────────────────────

class CareerBuilderScraper(BaseScraper):
    name = "CareerBuilder"

    async def fetch(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "keywords": job_title, "location": self.location or "",
            "posted": "today",
        })
        url = f"https://www.careerbuilder.com/jobs?{params}"
        soup = await self._pw_get(url, wait_selector="li[data-job-did]")
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select(
            "li[data-job-did], div.data-results-content"
        )[: self.max_results]
        for card in cards:
            title_el = (
                card.select_one("div.show-for-medium-up a")
                or card.select_one("a.job-title")
            )
            company_el = (
                card.select_one("div.data-details span:first-child")
                or card.select_one("[data-company]")
            )
            location_el = card.select_one("[data-location]")
            salary_el = card.select_one("[data-salary], [class*='salary']")

            if not title_el:
                continue

            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.careerbuilder.com" + href

            raw_salary = salary_el.get_text(strip=True) if salary_el else ""
            sal_text, sal_min, sal_max = extract_salary(raw_salary)

            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href, source=self.name,
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            ))
        return jobs


# ── Dice ──────────────────────────────────────────────────────────────────────

class DiceScraper(BaseScraper):
    name = "Dice"

    async def fetch(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "q": job_title, "countryCode": "US",
            "radius": "30", "radiusUnit": "mi",
            "datePosted": "ONE", "sort": "-postedDate",
        })
        url = f"https://www.dice.com/jobs?{params}"
        soup = await self._pw_get(
            url,
            wait_selector="dhi-job-search-job-card, div.search-card",
            wait_ms=4000,
        )
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select(
            "dhi-job-search-job-card, div.search-card"
        )[: self.max_results]
        for card in cards:
            title_el = (
                card.select_one("a.card-title-link")
                or card.select_one("[data-cy='card-title-link']")
                or card.select_one("a[id^='jobTitle']")
            )
            company_el = (
                card.select_one("a.company-name-link")
                or card.select_one("[data-cy='search-result-company-name']")
            )
            location_el = (
                card.select_one("span.search-result-location")
                or card.select_one("[data-cy='search-result-location']")
            )
            date_el = card.select_one("span.posted-date, [data-cy='card-posted-date']")
            salary_el = card.select_one("[data-cy='search-result-salary'], [class*='salary']")

            if not title_el:
                continue

            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.dice.com" + href

            posted = _parse_relative_date(date_el.get_text(strip=True) if date_el else "")
            raw_salary = salary_el.get_text(strip=True) if salary_el else ""
            sal_text, sal_min, sal_max = extract_salary(raw_salary)

            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href, source=self.name, posted=posted,
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            )
            if job.is_recent(self.hours_ago):
                jobs.append(job)
        return jobs


# ── The Muse ──────────────────────────────────────────────────────────────────

class TheMuseScraper(BaseScraper):
    name = "The Muse"
    _API = "https://www.themuse.com/api/public/jobs"

    async def fetch(self, job_title: str) -> List[Job]:
        data = await self._api_get(
            self._API,
            params={"page": 1, "descending": "true", "category": "IT"},
            json_response=True,
        )
        if not data:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.hours_ago)
        keyword = job_title.lower()
        jobs: List[Job] = []

        for item in data.get("results", []):
            title = item.get("name", "")
            if keyword not in title.lower():
                continue
            posted = _parse_iso(item.get("publication_date", ""))
            if posted and posted < cutoff:
                continue

            company = (
                item.get("company", {}).get("name", "Unknown")
                if isinstance(item.get("company"), dict) else "Unknown"
            )
            locations = item.get("locations", [])
            location = locations[0].get("name", "Remote") if locations else "Remote"
            url = item.get("refs", {}).get("landing_page", "")

            jobs.append(Job(
                title=title, company=company, location=location,
                url=url, source=self.name, posted=posted,
            ))
            if len(jobs) >= self.max_results:
                break
        return jobs


# ── Adzuna ────────────────────────────────────────────────────────────────────

class AdzunaScraper(BaseScraper):
    name = "Adzuna"

    def __init__(self, config: dict, browser=None):
        super().__init__(config, browser)
        board_cfg = config.get("job_boards", {}).get("adzuna", {})
        self.app_id = board_cfg.get("app_id", "")
        self.app_key = board_cfg.get("app_key", "")
        self.country = board_cfg.get("country", "us")

    async def fetch(self, job_title: str) -> List[Job]:
        if not self.app_id or not self.app_key:
            logger.warning("[Adzuna] app_id / app_key not set — skipping.")
            return []

        url = f"https://api.adzuna.com/v1/api/jobs/{self.country}/search/1"
        params = {
            "app_id": self.app_id, "app_key": self.app_key,
            "what": job_title, "max_days_old": "1",
            "results_per_page": self.max_results,
            "sort_by": "date", "content-type": "application/json",
        }
        if self.location:
            params["where"] = self.location

        data = await self._api_get(url, params=params, json_response=True)
        if not data:
            return []

        jobs: List[Job] = []
        for item in data.get("results", []):
            posted = _parse_iso(item.get("created", ""))
            desc = item.get("description", "")
            sal_text, sal_min, sal_max = extract_salary(desc[:500])
            # Adzuna may provide salary_min/max directly
            if not sal_min and item.get("salary_min"):
                sal_min = int(item["salary_min"])
            if not sal_max and item.get("salary_max"):
                sal_max = int(item["salary_max"])
            if sal_min and not sal_text:
                sal_text = (
                    f"${sal_min:,} – ${sal_max:,}" if sal_max and sal_max != sal_min
                    else f"${sal_min:,}"
                )

            job = Job(
                title=item.get("title", "N/A"),
                company=item.get("company", {}).get("display_name", "Unknown"),
                location=item.get("location", {}).get("display_name", self.location or "US"),
                url=item.get("redirect_url", ""),
                source=self.name, posted=posted,
                description=desc[:300],
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            )
            if job.is_recent(self.hours_ago):
                jobs.append(job)
        return jobs


# ── RemoteOK ──────────────────────────────────────────────────────────────────

class RemoteOKScraper(BaseScraper):
    name = "RemoteOK"
    _API = "https://remoteok.com/api"

    async def fetch(self, job_title: str) -> List[Job]:
        keyword = job_title.lower().replace(" ", "+")
        data = await self._api_get(
            f"{self._API}?tag={keyword}",
            headers={"Accept": "application/json"},
            json_response=True,
        )
        if not data or not isinstance(data, list):
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.hours_ago)
        jobs: List[Job] = []
        for item in data:
            if not isinstance(item, dict) or "position" not in item:
                continue
            epoch = item.get("epoch", 0)
            posted = datetime.fromtimestamp(int(epoch), tz=timezone.utc) if epoch else None
            if posted and posted < cutoff:
                continue
            slug = item.get("slug", "")
            url = f"https://remoteok.com/remote-jobs/{slug}" if slug else ""
            desc = BeautifulSoup(item.get("description", ""), "lxml").get_text()
            sal_text, sal_min, sal_max = extract_salary(desc[:500])

            jobs.append(Job(
                title=item.get("position", "N/A"),
                company=item.get("company", "Unknown"),
                location="Remote", url=url, source=self.name, posted=posted,
                remote=True, tags=item.get("tags", []),
                description=desc[:300],
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            ))
            if len(jobs) >= self.max_results:
                break
        return jobs


# ── Jobicy ────────────────────────────────────────────────────────────────────

class JobicyScraper(BaseScraper):
    name = "Jobicy"
    _API = "https://jobicy.com/api/v2/remote-jobs"

    async def fetch(self, job_title: str) -> List[Job]:
        data = await self._api_get(
            self._API,
            params={"count": self.max_results, "tag": job_title},
            json_response=True,
        )
        if not data:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.hours_ago)
        jobs: List[Job] = []
        for item in data.get("jobs", []):
            posted = _parse_iso(item.get("pubDate", ""))
            if posted and posted < cutoff:
                continue
            desc = BeautifulSoup(item.get("jobExcerpt", ""), "lxml").get_text()
            sal_text, sal_min, sal_max = extract_salary(
                item.get("annualSalaryMin", "") or item.get("annualSalaryMax", "") or desc[:500]
            )
            if not sal_min and item.get("annualSalaryMin"):
                try:
                    sal_min = int(float(item["annualSalaryMin"]))
                    sal_max = int(float(item.get("annualSalaryMax", sal_min)))
                    sal_text = sal_text or f"${sal_min:,}"
                except (ValueError, TypeError):
                    pass

            jobs.append(Job(
                title=item.get("jobTitle", "N/A"),
                company=item.get("companyName", "Unknown"),
                location=item.get("jobGeo", "Remote"),
                url=item.get("url", ""), source=self.name, posted=posted,
                remote=True, description=desc[:300],
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            ))
        return jobs


# ── Builtin ───────────────────────────────────────────────────────────────────

class BuiltinScraper(BaseScraper):
    """
    Builtin.com — tech-focused job board with strong presence in major metros.
    Uses Playwright; Builtin is JS-heavy.
    """
    name = "Builtin"

    async def fetch(self, job_title: str) -> List[Job]:
        encoded = urllib.parse.quote_plus(job_title)
        url = f"https://builtin.com/jobs/search?search={encoded}"
        soup = await self._pw_get(
            url,
            wait_selector="[data-id='job-card'], article[class*='JobCard']",
            wait_ms=3500,
        )
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select(
            "[data-id='job-card'], article[class*='JobCard'], "
            "div[class*='job-card'], li[class*='job-result']"
        )[: self.max_results]

        for card in cards:
            title_el = (
                card.select_one("h2 a, h3 a, [class*='job-title'] a")
                or card.select_one("a[class*='title']")
            )
            company_el = card.select_one(
                "[class*='company-name'], [class*='employer'], span[class*='company']"
            )
            location_el = card.select_one(
                "[class*='location'], span[class*='metro']"
            )
            salary_el = card.select_one("[class*='salary'], [class*='compensation']")

            if not title_el:
                continue

            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://builtin.com" + href

            raw_salary = salary_el.get_text(strip=True) if salary_el else ""
            sal_text, sal_min, sal_max = extract_salary(raw_salary)

            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href, source=self.name,
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            ))
        return jobs


# ── Wellfound (AngelList Talent) ──────────────────────────────────────────────

class WellfoundScraper(BaseScraper):
    """
    Wellfound (formerly AngelList Talent) — startup-heavy tech job board.
    Uses Playwright; site is SPA.
    """
    name = "Wellfound"

    async def fetch(self, job_title: str) -> List[Job]:
        encoded = urllib.parse.quote_plus(job_title)
        url = f"https://wellfound.com/jobs?q={encoded}"
        soup = await self._pw_get(
            url,
            wait_selector="[class*='JobListingCard'], [data-test='job-listing']",
            wait_ms=4000,
        )
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select(
            "[class*='JobListingCard'], [data-test='job-listing'], "
            "div[class*='styles_jobListing']"
        )[: self.max_results]

        for card in cards:
            title_el = (
                card.select_one("a[class*='title'], h2 a, h3 a")
                or card.select_one("[class*='JobTitle']")
            )
            company_el = card.select_one(
                "[class*='company'], [class*='startup-link'], a[class*='name']"
            )
            location_el = card.select_one(
                "[class*='location'], [class*='LocationTag']"
            )
            salary_el = card.select_one(
                "[class*='salary'], [class*='compensation'], [class*='Compensation']"
            )

            if not title_el:
                continue

            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://wellfound.com" + href

            raw_salary = salary_el.get_text(strip=True) if salary_el else ""
            sal_text, sal_min, sal_max = extract_salary(raw_salary)

            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else "US",
                url=href, source=self.name,
                salary_text=sal_text, salary_min=sal_min, salary_max=sal_max,
            ))
        return jobs


# ── Date helpers ──────────────────────────────────────────────────────────────

def _parse_iso(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _parse_relative_date(text: str) -> datetime | None:
    if not text:
        return None
    text = text.lower().strip()
    now = datetime.now(timezone.utc)
    if "just" in text or "today" in text or "hour" in text:
        return now
    m = re.search(r"(\d+)\s+day", text)
    if m:
        return now - timedelta(days=int(m.group(1)))
    return None


# ── Registry & factory ────────────────────────────────────────────────────────

SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "indeed":        IndeedScraper,
    "linkedin":      LinkedInScraper,
    "ziprecruiter":  ZipRecruiterScraper,
    "glassdoor":     GlassdoorScraper,
    "dice":          DiceScraper,
    "simplyhired":   SimplyHiredScraper,
    "monster":       MonsterScraper,
    "themuse":       TheMuseScraper,
    "adzuna":        AdzunaScraper,
    "careerbuilder": CareerBuilderScraper,
    "remoteok":      RemoteOKScraper,
    "jobicy":        JobicyScraper,
    "builtin":       BuiltinScraper,
    "wellfound":     WellfoundScraper,
}


def build_scrapers(config: dict, browser=None) -> list[BaseScraper]:
    """Return enabled scraper instances, injecting the shared async browser."""
    boards_cfg = config.get("job_boards", {})
    scrapers = []
    for key, cls in SCRAPER_REGISTRY.items():
        if boards_cfg.get(key, {}).get("enabled", False):
            scrapers.append(cls(config, browser))
            logger.info("Scraper enabled: %s", cls.name)
    return scrapers


async def run_all_scrapers_async(
    cfg: dict, browser, job_titles: list[str]
) -> tuple[list[Job], dict[str, int]]:
    """
    Run all enabled scrapers in parallel.
    Returns (jobs, board_counts) where board_counts maps board name → raw job count.
    A count of -1 indicates the scraper raised an exception.
    """
    scrapers = build_scrapers(cfg, browser)
    if not scrapers:
        logger.warning("No job boards enabled — check config.yaml.")
        return [], {}

    logger.info(
        "JobHelp v4 — %d board(s) × %d title(s) [parallel]",
        len(scrapers), len(job_titles),
    )
    tasks = [scraper.search_all(job_titles) for scraper in scrapers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_jobs: list[Job] = []
    board_counts: dict[str, int] = {}
    for scraper, result in zip(scrapers, results):
        if isinstance(result, Exception):
            logger.error("[%s] Scraper raised: %s", scraper.name, result)
            board_counts[scraper.name] = -1  # signals failure
        elif isinstance(result, list):
            logger.info("  %s → %d result(s)", scraper.name, len(result))
            board_counts[scraper.name] = len(result)
            all_jobs.extend(result)
    return all_jobs, board_counts


# ── Deduplication ─────────────────────────────────────────────────────────────

_COMPANY_STRIP = re.compile(
    r"\b(inc\.?|llc\.?|corp\.?|ltd\.?|co\.?|group|holdings|technologies|tech|solutions)\b",
    re.I,
)


def _normalise_company(name: str) -> str:
    return _COMPANY_STRIP.sub("", name).lower().strip(" ,.")


def fuzzy_deduplicate(jobs: list[Job], threshold: int = 88) -> list[Job]:
    """
    Cross-board deduplication using rapidfuzz token_sort_ratio.
    Two jobs are duplicates when:
      • normalised company similarity ≥ threshold  AND
      • title token_sort_ratio ≥ threshold
    Keeps the first occurrence (highest-quality source ordering from config).
    """
    unique: list[Job] = []
    for job in jobs:
        norm_company = _normalise_company(job.company)
        norm_title = job.title.lower().strip()
        is_dup = False
        for kept in unique:
            if (
                fuzz.token_sort_ratio(norm_title, kept.title.lower().strip()) >= threshold
                and fuzz.token_sort_ratio(
                    norm_company, _normalise_company(kept.company)
                ) >= threshold
            ):
                is_dup = True
                break
        if not is_dup:
            unique.append(job)

    removed = len(jobs) - len(unique)
    if removed:
        logger.info("Fuzzy dedup removed %d near-duplicate(s).", removed)
    return unique


# kept for backward-compat
def deduplicate(jobs: list[Job]) -> list[Job]:
    """Exact title + company dedup (legacy; use fuzzy_deduplicate in v3)."""
    seen: set[tuple] = set()
    unique: list[Job] = []
    for job in jobs:
        key = (job.title.lower().strip(), job.company.lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique
