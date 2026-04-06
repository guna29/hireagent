"""Wellfound (formerly AngelList Talent) job scraper.

Uses Wellfound's public GraphQL API to fetch startup job listings.
Filters for software engineering roles, stores results in the HireAgent DB.

Note: Wellfound has rate limiting. We use conservative delays and only
fetch the first few pages to avoid blocks.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

from hireagent.database import get_connection, init_db

log = logging.getLogger(__name__)

_GRAPHQL_URL = "https://wellfound.com/graphql"
_JOB_BASE = "https://wellfound.com/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://wellfound.com",
    "Referer": "https://wellfound.com/jobs",
}

# GraphQL query for job listings — uses the public slug-based search
_JOBS_QUERY = """
query JobSearchResults($query: String!, $page: Int) {
  talent {
    jobListings(query: $query, page: $page) {
      pageCount
      jobs {
        id
        title
        slug
        description
        remote
        locationNames
        jobType
        compensation
        equity
        startup {
          name
          slug
        }
        applyUrl
        liveStartAt
      }
    }
  }
}
"""

# Search queries targeting SWE roles
_QUERIES = [
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "software developer",
]

_SKIP_TITLE_KEYWORDS = {
    "senior", " sr ", "sr.", "staff", "principal", "lead", "manager",
    "director", "vp ", "intern", "internship", "sales", "marketing",
}

_SWE_TITLE_KEYWORDS = {
    "engineer", "developer", "sde", "swe", "software", "backend",
    "frontend", "full stack", "fullstack", "platform", "infrastructure",
    "devops", "mobile", "ios", "android", "data engineer", "ml engineer",
    "ai engineer",
}


def _is_swe_entry_title(title: str) -> bool:
    t = title.lower()
    if any(k in t for k in _SKIP_TITLE_KEYWORDS):
        return False
    return any(k in t for k in _SWE_TITLE_KEYWORDS)


def _graphql_search(query: str, page: int = 1, retries: int = 2) -> dict | None:
    """POST a GraphQL query to Wellfound."""
    payload = json.dumps({
        "query": _JOBS_QUERY,
        "variables": {"query": query, "page": page},
    }).encode("utf-8")

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(_GRAPHQL_URL, data=payload, headers=_HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 403) and attempt < retries:
                wait = 15 * (attempt + 1)
                log.warning("Wellfound rate-limited (%d), waiting %ds", e.code, wait)
                time.sleep(wait)
            else:
                log.error("Wellfound HTTP %d for query '%s' page %d", e.code, query, page)
                return None
        except Exception as e:
            if attempt < retries:
                time.sleep(5)
            else:
                log.error("Wellfound fetch failed: %s", e)
                return None
    return None


def _extract_jobs(data: dict) -> list[dict]:
    try:
        return data["data"]["talent"]["jobListings"]["jobs"] or []
    except (KeyError, TypeError):
        return []


def _extract_page_count(data: dict) -> int:
    try:
        return int(data["data"]["talent"]["jobListings"]["pageCount"] or 1)
    except (KeyError, TypeError, ValueError):
        return 1


def _store_jobs(jobs: list[dict]) -> tuple[int, int]:
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    new = existing = 0

    for job in jobs:
        try:
            title = (job.get("title") or "").strip()
            if not title or not _is_swe_entry_title(title):
                continue

            startup = job.get("startup") or {}
            company = (startup.get("name") or "").strip()
            startup_slug = startup.get("slug", "")

            # Build apply URL
            apply_url = job.get("applyUrl") or ""
            job_slug = job.get("slug") or str(job.get("id") or "")
            if not apply_url and startup_slug and job_slug:
                apply_url = f"https://wellfound.com/company/{startup_slug}/jobs/{job_slug}"
            if not apply_url:
                continue

            # Location
            location_parts = []
            if job.get("remote"):
                location_parts.append("Remote")
            locs = job.get("locationNames") or []
            if isinstance(locs, list):
                location_parts.extend(locs[:2])
            elif isinstance(locs, str):
                location_parts.append(locs)
            location = ", ".join(location_parts) or "Remote"

            description = job.get("description") or ""
            full_desc = description if len(description) > 200 else None
            detail_scraped_at = now if full_desc else None

            salary = None
            comp = job.get("compensation")
            equity = job.get("equity")
            if comp:
                salary = str(comp)
            elif equity:
                salary = f"equity: {equity}"

            try:
                conn.execute(
                    "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                    "discovered_at, full_description, application_url, detail_scraped_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        apply_url, title, salary,
                        (description[:500] if description else None),
                        location, "wellfound", "wellfound",
                        now, full_desc, apply_url, detail_scraped_at,
                    ),
                )
                new += 1
            except Exception:
                existing += 1
        except Exception as e:
            log.debug("Wellfound store error: %s", e)

    conn.commit()
    return new, existing


def run_wellfound_discovery(max_pages: int = 3) -> dict:
    """Main entry point: scrape Wellfound startup jobs and store results."""
    init_db()
    total_new = total_existing = 0

    for query in _QUERIES:
        log.info("Wellfound searching: '%s'", query)

        # Fetch first page to get page count
        data = _graphql_search(query, page=1)
        if not data:
            log.warning("Wellfound: no data for query '%s'", query)
            time.sleep(3)
            continue

        jobs = _extract_jobs(data)
        page_count = min(_extract_page_count(data), max_pages)

        n, e = _store_jobs(jobs)
        total_new += n
        total_existing += e
        log.info("Wellfound '%s' page 1: %d jobs, %d new, %d dupes", query, len(jobs), n, e)

        for page in range(2, page_count + 1):
            time.sleep(3)  # polite delay
            data = _graphql_search(query, page=page)
            if not data:
                break
            jobs = _extract_jobs(data)
            n, e = _store_jobs(jobs)
            total_new += n
            total_existing += e
            log.info("Wellfound '%s' page %d: %d jobs, %d new, %d dupes",
                     query, page, len(jobs), n, e)

        time.sleep(5)  # between queries

    log.info("Wellfound total: %d new, %d existing", total_new, total_existing)
    return {"new": total_new, "existing": total_existing}
