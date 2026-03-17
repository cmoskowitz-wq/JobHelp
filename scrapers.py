"""
scrapers.py — JobHelp Version 2
Headless-browser scrapers (Playwright) for HTML-based boards,
plus requests-based scrapers for boards that offer public APIs.

Architecture:
  - HTML boards  → _pw_get() uses Playwright; page rendered before parsing
  - API boards   → _api_get() uses requests; plain JSON/RSS fetch
  - BaseScraper  → shared init, search_all(), rate-limit sleep
  - SCRAPER_REGISTRY + build_scrapers() unchanged interface (browser arg added)
"""

from __future__ import annotations

import logging
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

_API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/html, */*",
}

REQUEST_TIMEOUT = 20


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
        }


# ── Base scraper ──────────────────────────────────────────────────────────────

class BaseScraper:
    name: str = "base"

    def __init__(self, config: dict, browser=None):
        self.config = config
        self.browser = browser
        search_cfg = config.get("search", {})
        self.hours_ago: int = search_cfg.get("hours_ago", 24)
        self.location: str = search_cfg.get("location", "")
        self.max_results: int = search_cfg.get("results_per_board", 25)

    # ── Playwright fetch (HTML boards) ────────────────────────────────────────

    def _pw_get(
        self,
        url: str,
        wait_selector: str | None = None,
        wait_ms: int = 2500,
    ) -> BeautifulSoup | None:
        """
        Navigate to *url* with a headless Chromium page and return parsed HTML.
        Each call gets its own browser context (clean cookies/fingerprint).
        """
        if self.browser is None:
            logger.warning("[%s] No browser instance available.", self.name)
            return None
        ctx = None
        try:
            ctx = self.browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = ctx.new_page()
            # Block ads/trackers to speed up loads
            page.route(
                "**/{ads,analytics,doubleclick,googlesyndication}**",
                lambda route: route.abort(),
            )
            page.goto(url, timeout=35_000, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8_000)
                except Exception:
                    pass  # best-effort; parse whatever loaded
            else:
                page.wait_for_timeout(wait_ms)
            return BeautifulSoup(page.content(), "lxml")
        except Exception as exc:
            logger.warning("[%s] Playwright error on %s: %s", self.name, url, exc)
            return None
        finally:
            if ctx:
                ctx.close()

    # ── requests fetch (API boards) ───────────────────────────────────────────

    def _api_get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        json_response: bool = False,
    ):
        """Thin requests wrapper with one retry."""
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

    # ── search loop ───────────────────────────────────────────────────────────

    def fetch(self, job_title: str) -> List[Job]:
        raise NotImplementedError

    def search_all(self, job_titles: List[str]) -> List[Job]:
        results: List[Job] = []
        for title in job_titles:
            try:
                jobs = self.fetch(title)
                for j in jobs:
                    j.search_term = title
                results.extend(jobs)
                time.sleep(1.5)  # polite rate limiting between titles
            except Exception as exc:
                logger.error("[%s] Error searching '%s': %s", self.name, title, exc)
        return results


# ── Indeed ────────────────────────────────────────────────────────────────────

class IndeedScraper(BaseScraper):
    """Playwright — navigates Indeed job search results page."""
    name = "Indeed"

    def fetch(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "q": job_title,
            "sort": "date",
            "fromage": "1",
            "l": self.location or "",
        })
        url = f"https://www.indeed.com/jobs?{params}"
        soup = self._pw_get(url, wait_selector="[data-testid='jobsearch-ResultsList']")
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

            if not title_el:
                continue

            href = ""
            if link_el:
                jk = link_el.get("data-jk", "")
                href = f"https://www.indeed.com/viewjob?jk={jk}" if jk else link_el.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.indeed.com" + href

            # Try to parse posted date from nearby element
            date_el = card.select_one("[data-testid='myJobsStateDate'], span.date")
            posted = _parse_relative_date(date_el.get_text(strip=True) if date_el else "")

            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href,
                source=self.name,
                posted=posted,
            )
            if job.is_recent(self.hours_ago):
                jobs.append(job)

        return jobs


# ── LinkedIn ──────────────────────────────────────────────────────────────────

class LinkedInScraper(BaseScraper):
    """Playwright — uses LinkedIn's public (unauthenticated) job search."""
    name = "LinkedIn"

    def fetch(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "keywords": job_title,
            "location": self.location or "United States",
            "f_TPR": "r86400",   # last 24 h
            "position": 1,
            "pageNum": 0,
        })
        url = f"https://www.linkedin.com/jobs/search?{params}"
        soup = self._pw_get(
            url,
            wait_selector="ul.jobs-search__results-list, div.jobs-search-results-grid",
            wait_ms=3000,
        )
        if not soup:
            return []

        jobs: List[Job] = []
        cards = soup.select(
            "li.jobs-search-results__list-item, "
            "div.base-card, "
            "li.job-search-card"
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
            link_el = card.select_one("a.base-card__full-link, a.job-search-card__title-link")
            time_el = card.select_one("time[datetime]")

            if not title_el or not link_el:
                continue

            posted = None
            if time_el and time_el.get("datetime"):
                posted = _parse_iso(time_el["datetime"])

            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=link_el["href"].split("?")[0],
                source=self.name,
                posted=posted,
            )
            if job.is_recent(self.hours_ago):
                jobs.append(job)

        return jobs


# ── ZipRecruiter ──────────────────────────────────────────────────────────────

class ZipRecruiterScraper(BaseScraper):
    """Playwright — ZipRecruiter candidate job search."""
    name = "ZipRecruiter"

    def fetch(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "search": job_title,
            "location": self.location or "",
            "days": "1",
        })
        url = f"https://www.ziprecruiter.com/candidate/search?{params}"
        soup = self._pw_get(
            url,
            wait_selector="article.job_result, div[data-testid='job-card']",
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

            if not title_el:
                continue

            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.ziprecruiter.com" + href

            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href,
                source=self.name,
            )
            jobs.append(job)

        return jobs


# ── Glassdoor ─────────────────────────────────────────────────────────────────

class GlassdoorScraper(BaseScraper):
    """Playwright — Glassdoor public job search."""
    name = "Glassdoor"

    def fetch(self, job_title: str) -> List[Job]:
        encoded = urllib.parse.quote_plus(job_title)
        url = (
            f"https://www.glassdoor.com/Job/jobs.htm"
            f"?sc.keyword={encoded}&fromAge=1&sort.sortType=date&sort.descending=true"
        )
        soup = self._pw_get(
            url,
            wait_selector="li[data-test='jobListing'], li.react-job-listing",
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
            link_el = card.select_one("a[href*='/job-listing/'], a[href*='/partner/jobListing']")

            if not title_el:
                continue

            href = ""
            if link_el:
                href = link_el.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.glassdoor.com" + href

            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href,
                source=self.name,
            )
            jobs.append(job)

        return jobs


# ── SimplyHired ───────────────────────────────────────────────────────────────

class SimplyHiredScraper(BaseScraper):
    """Playwright — SimplyHired job search."""
    name = "SimplyHired"

    def fetch(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "q": job_title,
            "l": self.location or "",
            "dateposted": "1",
        })
        url = f"https://www.simplyhired.com/search?{params}"
        soup = self._pw_get(
            url,
            wait_selector="div[data-testid='job-card'], article.SerpJob",
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

            if not title_el:
                continue

            href = ""
            if link_el:
                href = link_el.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.simplyhired.com" + href

            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href,
                source=self.name,
            )
            jobs.append(job)

        return jobs


# ── Monster ───────────────────────────────────────────────────────────────────

class MonsterScraper(BaseScraper):
    """Playwright — Monster job search."""
    name = "Monster"

    def fetch(self, job_title: str) -> List[Job]:
        encoded = urllib.parse.quote_plus(job_title)
        loc = urllib.parse.quote_plus(self.location or "")
        url = f"https://www.monster.com/jobs/search?q={encoded}&where={loc}&tm=1"
        soup = self._pw_get(
            url,
            wait_selector="div[data-testid='JobCard'], section.card-content",
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

            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href,
                source=self.name,
            )
            jobs.append(job)

        return jobs


# ── CareerBuilder ─────────────────────────────────────────────────────────────

class CareerBuilderScraper(BaseScraper):
    """Playwright — CareerBuilder job search."""
    name = "CareerBuilder"

    def fetch(self, job_title: str) -> List[Job]:
        params = urllib.parse.urlencode({
            "keywords": job_title,
            "location": self.location or "",
            "posted": "today",
        })
        url = f"https://www.careerbuilder.com/jobs?{params}"
        soup = self._pw_get(url, wait_selector="li[data-job-did]")
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

            if not title_el:
                continue

            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.careerbuilder.com" + href

            job = Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "Unknown",
                location=location_el.get_text(strip=True) if location_el else self.location or "US",
                url=href,
                source=self.name,
            )
            jobs.append(job)

        return jobs


# ── Dice ──────────────────────────────────────────────────────────────────────

class DiceScraper(BaseScraper):
    """API — Dice internal search API (no credentials needed)."""
    name = "Dice"
    _API = "https://job-search-api.svc.dhigroupinc.com/v1/dice/jobs/search"

    def fetch(self, job_title: str) -> List[Job]:
        params = {
            "q": job_title,
            "countryCode2": "US",
            "radius": "30",
            "radiusUnit": "mi",
            "page": 1,
            "pageSize": self.max_results,
            "filters.postedDate": "ONE",
            "sort": "-postedDate",
        }
        if self.location:
            params["location"] = self.location

        data = self._api_get(self._API, params=params, json_response=True)
        if not data:
            return []

        jobs: List[Job] = []
        for item in data.get("data", []):
            posted = _parse_iso(item.get("postedDate", ""))
            job_id = item.get("id", "")
            url = f"https://www.dice.com/job-detail/{job_id}" if job_id else ""

            job = Job(
                title=item.get("title", "N/A"),
                company=item.get("companyPageUrl", item.get("company", "Unknown")),
                location=item.get("location", self.location or "US"),
                url=url,
                source=self.name,
                posted=posted,
                remote="Remote" in item.get("workplaceTypes", []),
            )
            if job.is_recent(self.hours_ago):
                jobs.append(job)

        return jobs


# ── The Muse ──────────────────────────────────────────────────────────────────

class TheMuseScraper(BaseScraper):
    """API — The Muse free public API (no key required)."""
    name = "The Muse"
    _API = "https://www.themuse.com/api/public/jobs"

    def fetch(self, job_title: str) -> List[Job]:
        data = self._api_get(
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
                if isinstance(item.get("company"), dict)
                else "Unknown"
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
    """API — Adzuna official API (free tier)."""
    name = "Adzuna"

    def __init__(self, config: dict, browser=None):
        super().__init__(config, browser)
        board_cfg = config.get("job_boards", {}).get("adzuna", {})
        self.app_id = board_cfg.get("app_id", "")
        self.app_key = board_cfg.get("app_key", "")
        self.country = board_cfg.get("country", "us")

    def fetch(self, job_title: str) -> List[Job]:
        if not self.app_id or not self.app_key:
            logger.warning("[Adzuna] app_id / app_key not set — skipping.")
            return []

        url = f"https://api.adzuna.com/v1/api/jobs/{self.country}/search/1"
        params = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "what": job_title,
            "max_days_old": "1",
            "results_per_page": self.max_results,
            "sort_by": "date",
            "content-type": "application/json",
        }
        if self.location:
            params["where"] = self.location

        data = self._api_get(url, params=params, json_response=True)
        if not data:
            return []

        jobs: List[Job] = []
        for item in data.get("results", []):
            posted = _parse_iso(item.get("created", ""))
            job = Job(
                title=item.get("title", "N/A"),
                company=item.get("company", {}).get("display_name", "Unknown"),
                location=item.get("location", {}).get("display_name", self.location or "US"),
                url=item.get("redirect_url", ""),
                source=self.name,
                posted=posted,
                description=item.get("description", "")[:300],
            )
            if job.is_recent(self.hours_ago):
                jobs.append(job)

        return jobs


# ── RemoteOK ──────────────────────────────────────────────────────────────────

class RemoteOKScraper(BaseScraper):
    """API — RemoteOK free public JSON API."""
    name = "RemoteOK"
    _API = "https://remoteok.com/api"

    def fetch(self, job_title: str) -> List[Job]:
        keyword = job_title.lower().replace(" ", "+")
        data = self._api_get(
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
            jobs.append(Job(
                title=item.get("position", "N/A"),
                company=item.get("company", "Unknown"),
                location="Remote",
                url=url,
                source=self.name,
                posted=posted,
                remote=True,
                tags=item.get("tags", []),
                description=BeautifulSoup(
                    item.get("description", ""), "lxml"
                ).get_text()[:300],
            ))
            if len(jobs) >= self.max_results:
                break

        return jobs


# ── Jobicy ────────────────────────────────────────────────────────────────────

class JobicyScraper(BaseScraper):
    """API — Jobicy free public API."""
    name = "Jobicy"
    _API = "https://jobicy.com/api/v2/remote-jobs"

    def fetch(self, job_title: str) -> List[Job]:
        data = self._api_get(
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
            jobs.append(Job(
                title=item.get("jobTitle", "N/A"),
                company=item.get("companyName", "Unknown"),
                location=item.get("jobGeo", "Remote"),
                url=item.get("url", ""),
                source=self.name,
                posted=posted,
                remote=True,
                description=BeautifulSoup(
                    item.get("jobExcerpt", ""), "lxml"
                ).get_text()[:300],
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
    """Parse Indeed-style relative date strings like '2 days ago', 'Just posted'."""
    if not text:
        return None
    text = text.lower().strip()
    now = datetime.now(timezone.utc)
    if "just" in text or "today" in text or "hour" in text:
        return now
    try:
        import re
        m = re.search(r"(\d+)\s+day", text)
        if m:
            return now - timedelta(days=int(m.group(1)))
    except Exception:
        pass
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
}


def build_scrapers(config: dict, browser=None) -> List[BaseScraper]:
    """Return enabled scraper instances, injecting the shared browser."""
    boards_cfg = config.get("job_boards", {})
    scrapers = []
    for key, cls in SCRAPER_REGISTRY.items():
        if boards_cfg.get(key, {}).get("enabled", False):
            scrapers.append(cls(config, browser))
            logger.info("Scraper enabled: %s", cls.name)
    return scrapers


def deduplicate(jobs: List[Job]) -> List[Job]:
    """Remove jobs with the same title + company (cross-board dedup)."""
    seen: set[tuple] = set()
    unique: List[Job] = []
    for job in jobs:
        key = (job.title.lower().strip(), job.company.lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique
