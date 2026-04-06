"""Otto.careers AI-focused job board scraper.

Scrapes https://otto.careers for AI/ML and software engineering roles.
Uses HTTP requests to parse the job listings page.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from html.parser import HTMLParser

from hireagent.database import get_connection, init_db

log = logging.getLogger(__name__)

_BASE_URL = "https://otto.careers"
_JOBS_URL = "https://otto.careers/jobs"

# Try the API endpoint first; fall back to HTML scraping
_API_URLS = [
    "https://otto.careers/api/jobs",
    "https://otto.careers/api/v1/jobs",
    "https://otto.careers/jobs.json",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Referer": "https://otto.careers/",
}

_SWE_TITLE_KEYWORDS = {
    "engineer", "developer", "sde", "swe", "software", "backend",
    "frontend", "full stack", "fullstack", "platform", "infrastructure",
    "devops", "mobile", "ios", "android", "ml engineer", "ai engineer",
    "machine learning", "data engineer", "research engineer",
}

_SKIP_TITLE_KEYWORDS = {
    "senior", " sr ", "sr.", "staff", "principal", "lead", "manager",
    "director", "vp ", "intern", "internship", "sales", "marketing",
}


def _is_target_title(title: str) -> bool:
    t = title.lower()
    if any(k in t for k in _SKIP_TITLE_KEYWORDS):
        return False
    return any(k in t for k in _SWE_TITLE_KEYWORDS)


def _fetch_url(url: str, retries: int = 2) -> bytes | None:
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(10 * (attempt + 1))
            else:
                log.debug("Otto HTTP %d: %s", e.code, url)
                return None
        except Exception as e:
            if attempt < retries:
                time.sleep(5)
            else:
                log.debug("Otto fetch error: %s", e)
                return None
    return None


class _JobHTMLParser(HTMLParser):
    """Parse job listings from otto.careers HTML page."""

    def __init__(self):
        super().__init__()
        self.jobs: list[dict] = []
        self._current: dict | None = None
        self._in_title = False
        self._in_company = False
        self._in_location = False
        self._capture = False
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "") or ""
        href = attrs_dict.get("href", "") or ""

        # Look for job card links
        if tag == "a" and href and "/jobs/" in href:
            url = href if href.startswith("http") else f"{_BASE_URL}{href}"
            self._current = {"url": url, "title": "", "company": "", "location": ""}

        if self._current:
            if "title" in cls or "job-title" in cls or "position" in cls:
                self._in_title = True
            elif "company" in cls or "employer" in cls:
                self._in_company = True
            elif "location" in cls:
                self._in_location = True

    def handle_endtag(self, tag):
        self._in_title = False
        self._in_company = False
        self._in_location = False

    def handle_data(self, data):
        data = data.strip()
        if not data or not self._current:
            return
        if self._in_title:
            self._current["title"] = data
        elif self._in_company:
            self._current["company"] = data
        elif self._in_location:
            self._current["location"] = data

        # Flush completed job card
        if self._current.get("title") and self._current.get("url"):
            self.jobs.append(self._current)
            self._current = None


def _parse_jobs_from_html(html_bytes: bytes) -> list[dict]:
    """Try to extract jobs from HTML using JSON-LD or parser."""
    html = html_bytes.decode("utf-8", errors="replace")

    jobs = []

    # Try JSON-LD structured data first
    for match in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                              html, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(match.group(1))
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") in ("JobPosting", "jobPosting"):
                    url = item.get("url") or item.get("@id") or ""
                    if not url:
                        continue
                    jobs.append({
                        "url": url,
                        "title": item.get("title") or "",
                        "company": (item.get("hiringOrganization") or {}).get("name") or "",
                        "location": str(item.get("jobLocation") or ""),
                        "description": item.get("description") or "",
                        "salary": str(item.get("baseSalary") or "") or None,
                    })
        except Exception:
            pass

    if jobs:
        return jobs

    # Try __NEXT_DATA__ / window.__INITIAL_STATE__
    for pattern in (
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
    ):
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                # Walk the JSON tree looking for job-like objects
                raw_jobs = _extract_jobs_from_dict(data)
                if raw_jobs:
                    return raw_jobs
            except Exception:
                pass

    # Fallback: HTML parser
    parser = _JobHTMLParser()
    try:
        parser.feed(html)
        return parser.jobs
    except Exception:
        return []


def _extract_jobs_from_dict(data, depth: int = 0) -> list[dict]:
    """Recursively search a dict/list for job objects."""
    if depth > 8:
        return []
    jobs = []
    if isinstance(data, list):
        for item in data[:200]:
            jobs.extend(_extract_jobs_from_dict(item, depth + 1))
    elif isinstance(data, dict):
        # Job-like if it has title + some url
        if data.get("title") and (data.get("url") or data.get("applyUrl") or data.get("slug")):
            title = str(data.get("title") or "")
            url = (data.get("url") or data.get("applyUrl") or
                   (f"{_BASE_URL}/jobs/{data['slug']}" if data.get("slug") else ""))
            if url:
                jobs.append({
                    "url": url,
                    "title": title,
                    "company": str(data.get("company") or data.get("companyName") or ""),
                    "location": str(data.get("location") or data.get("locationName") or ""),
                    "description": str(data.get("description") or data.get("body") or ""),
                    "salary": str(data.get("salary") or data.get("compensation") or "") or None,
                })
        else:
            for v in data.values():
                jobs.extend(_extract_jobs_from_dict(v, depth + 1))
    return jobs


def _store_jobs(jobs: list[dict]) -> tuple[int, int]:
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    new = existing = 0

    for job in jobs:
        try:
            title = (job.get("title") or "").strip()
            if not title or not _is_target_title(title):
                continue

            url = (job.get("url") or "").strip()
            if not url:
                continue

            description = job.get("description") or ""
            full_desc = description if len(description) > 200 else None
            detail_scraped_at = now if full_desc else None

            location = (job.get("location") or "Remote").strip() or "Remote"
            salary = job.get("salary") or None
            if salary and len(str(salary)) > 100:
                salary = None

            try:
                conn.execute(
                    "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                    "discovered_at, full_description, application_url, detail_scraped_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        url, title, salary,
                        (description[:500] if description else None),
                        location, "otto.careers", "otto",
                        now, full_desc, url, detail_scraped_at,
                    ),
                )
                new += 1
            except Exception:
                existing += 1
        except Exception as e:
            log.debug("Otto store error: %s", e)

    conn.commit()
    return new, existing


def run_otto_discovery() -> dict:
    """Main entry point: scrape otto.careers and store results."""
    init_db()

    # Try JSON API endpoints first
    for api_url in _API_URLS:
        body = _fetch_url(api_url)
        if body:
            try:
                data = json.loads(body)
                jobs = data if isinstance(data, list) else data.get("jobs", [])
                if jobs:
                    log.info("Otto API (%s): %d jobs", api_url, len(jobs))
                    n, e = _store_jobs(jobs)
                    log.info("Otto: %d new, %d existing", n, e)
                    return {"new": n, "existing": e}
            except Exception:
                pass

    # Fall back to HTML scraping
    log.info("Otto: trying HTML scrape of %s", _JOBS_URL)
    body = _fetch_url(_JOBS_URL)
    if not body:
        log.warning("Otto: failed to fetch job listings page")
        return {"new": 0, "existing": 0}

    jobs = _parse_jobs_from_html(body)
    log.info("Otto HTML parse: %d jobs found", len(jobs))

    if not jobs:
        log.warning("Otto: could not parse any jobs from HTML")
        return {"new": 0, "existing": 0}

    n, e = _store_jobs(jobs)
    log.info("Otto: %d new, %d existing", n, e)
    return {"new": n, "existing": e}
