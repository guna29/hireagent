"""Work at a Startup (YC) job scraper.

Scrapes https://www.workatastartup.com/jobs using their public JSON API.
Filters for software engineering roles and stores results in the HireAgent DB.
No authentication required — the API is publicly accessible.
"""

from __future__ import annotations

import logging
import time
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone

from hireagent.database import get_connection, init_db

log = logging.getLogger(__name__)

_BASE_URL = "https://www.workatastartup.com"
_JOBS_API = "https://www.workatastartup.com/jobs.json"

# Query params: role=eng filters to engineering, remote=true for remote-friendly
_SEARCH_PARAMS = [
    {"role": "eng", "remote": "true", "job_type": "fulltime"},
    {"role": "eng", "remote": "false", "job_type": "fulltime"},
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.workatastartup.com/jobs",
    "X-Requested-With": "XMLHttpRequest",
}

# Title keywords that indicate non-SWE roles to skip
_SKIP_TITLE_KEYWORDS = {
    "sales", "marketing", "recruiter", "hr ", "human resources",
    "finance", "accounting", "legal", "counsel", "operations manager",
    "product manager", "ux designer", "ui designer", "graphic designer",
    "content writer", "technical writer", "customer success",
    "account executive", "business development",
}

# Must contain at least one of these to be considered a SWE role
_SWE_TITLE_KEYWORDS = {
    "engineer", "developer", "sde", "swe", "software", "backend",
    "frontend", "full stack", "fullstack", "full-stack", "platform",
    "infrastructure", "devops", "site reliability", "sre", "data engineer",
    "ml engineer", "ai engineer", "machine learning", "mobile", "ios",
    "android", "web", "api", "cloud", "systems", "application",
}


def _is_swe_title(title: str) -> bool:
    t = title.lower()
    if any(k in t for k in _SKIP_TITLE_KEYWORDS):
        return False
    return any(k in t for k in _SWE_TITLE_KEYWORDS)


def _fetch_jobs_page(params: dict, retries: int = 2) -> list[dict]:
    """Fetch jobs from the WaaS JSON API."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{_JOBS_API}?{query}"

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                # API returns {"jobs": [...]} or just a list
                if isinstance(data, dict):
                    return data.get("jobs", []) or data.get("data", [])
                if isinstance(data, list):
                    return data
                return []
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                wait = 10 * (attempt + 1)
                log.warning("WaaS rate-limited, waiting %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
            else:
                log.error("WaaS HTTP error %s: %s", e.code, url)
                return []
        except Exception as e:
            if attempt < retries:
                time.sleep(5)
            else:
                log.error("WaaS fetch failed: %s", e)
                return []
    return []


def _store_jobs(jobs: list[dict]) -> tuple[int, int]:
    """Store scraped jobs into the DB. Returns (new, existing)."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    new = existing = 0

    for job in jobs:
        try:
            # Build apply URL: prefer direct URL, fall back to WaaS job page
            job_id = job.get("id") or job.get("slug") or ""
            company_slug = (job.get("company") or {}).get("slug", "")
            if job.get("url"):
                apply_url = job["url"]
            elif job_id and company_slug:
                apply_url = f"{_BASE_URL}/companies/{company_slug}/jobs/{job_id}"
            elif job_id:
                apply_url = f"{_BASE_URL}/jobs/{job_id}"
            else:
                continue

            title = (job.get("title") or "").strip()
            if not title:
                continue

            if not _is_swe_title(title):
                continue

            company_info = job.get("company") or {}
            company = (company_info.get("name") or "").strip()

            location_parts = []
            if job.get("remote"):
                location_parts.append("Remote")
            if job.get("location"):
                location_parts.append(job["location"])
            location = ", ".join(location_parts) or "Remote"

            description = job.get("description") or job.get("body") or ""
            # WaaS descriptions are often HTML — keep as-is, enrichment will clean
            full_desc = description if len(description) > 200 else None
            detail_scraped_at = now if full_desc else None

            salary = None
            if job.get("salary_min") and job.get("salary_max"):
                salary = f"${int(job['salary_min']):,}-${int(job['salary_max']):,}/year"
            elif job.get("equity_min"):
                salary = f"equity: {job['equity_min']}-{job.get('equity_max', '')}%"

            try:
                conn.execute(
                    "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                    "discovered_at, full_description, application_url, detail_scraped_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        apply_url, title, salary,
                        (description[:500] if description else None),
                        location, "workatastartup", "workatastartup",
                        now, full_desc, apply_url, detail_scraped_at,
                    ),
                )
                new += 1
            except Exception:
                existing += 1
        except Exception as e:
            log.debug("WaaS store error for job: %s", e)

    conn.commit()
    return new, existing


def run_workatastartup_discovery() -> dict:
    """Main entry point: scrape YC Work at a Startup and store results."""
    init_db()
    total_new = total_existing = 0

    for params in _SEARCH_PARAMS:
        label = f"remote={params.get('remote')}"
        log.info("WaaS scraping: %s", label)
        jobs = _fetch_jobs_page(params)
        if not jobs:
            log.warning("WaaS: 0 jobs returned for %s", label)
            continue

        log.info("WaaS: %d jobs fetched (%s)", len(jobs), label)
        n, e = _store_jobs(jobs)
        total_new += n
        total_existing += e
        log.info("WaaS [%s]: %d new, %d dupes", label, n, e)

        # Be polite between requests
        time.sleep(2)

    log.info("WaaS total: %d new, %d existing", total_new, total_existing)
    return {"new": total_new, "existing": total_existing}
