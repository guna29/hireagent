"""ScoutBetter job discovery — scrapes scoutbetter.jobs via their REST API.

Authentication:
  Set SCOUTBETTER_EMAIL and SCOUTBETTER_PASSWORD in your .env file.
  The module logs in, caches the Bearer token for the session, and
  paginates through all matching jobs.

Supported filters (via searches.yaml under scoutbetter: key):
  work_mode:       list of ints  1=Remote, 2=Hybrid, 3=Onsite
  contract_type:   list of ints  1=Full Time, 2=Part Time, 3=Contract,
                                 4=Internship, 5=Temporary
  lookup_term:     str           search keywords
  posted_within_days: int        only jobs posted within N days
  max_pages:       int           page cap (default 10)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse, parse_qs

log = logging.getLogger(__name__)

BASE_URL = "https://scoutbetter-production-webapp.azurewebsites.net/api/v1"
SITE_LABEL = "scoutbetter"

# Work mode labels for logging
_WORK_MODE = {1: "Remote", 2: "Hybrid", 3: "Onsite"}
_CONTRACT = {1: "Full Time", 2: "Part Time", 3: "Contract", 4: "Internship", 5: "Temporary"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _login(email: str, password: str) -> str:
    """Log in and return a Bearer token. Raises on failure."""
    import urllib.request, json

    payload = json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/auth/login/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())

    token = body.get("access") or body.get("token") or body.get("access_token")
    if not token:
        raise ValueError(f"Login succeeded but no token in response: {list(body.keys())}")
    log.info("[scoutbetter] Authenticated as %s", email)
    return token


# ---------------------------------------------------------------------------
# Job fetching
# ---------------------------------------------------------------------------

def _build_params(cfg: dict, page_url: str | None = None) -> str:
    """Build query string from config dict, or return existing page_url params."""
    if page_url:
        parsed = urlparse(page_url)
        return parsed.query

    params: list[tuple[str, str]] = [("applied", "false")]

    lookup_term = cfg.get("lookup_term", "")
    if lookup_term:
        params.append(("lookup_term", str(lookup_term)))

    for wm in cfg.get("work_mode", []):
        params.append(("work_mode", str(wm)))

    for ct in cfg.get("contract_type", []):
        params.append(("contract_type", str(ct)))

    posted_days = cfg.get("posted_within_days")
    if posted_days:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(posted_days))).strftime("%Y-%m-%d")
        params.append(("posted_at", cutoff))
        params.append(("posted_at_lookup", "after"))

    params.append(("filter_type", "recent"))

    return urlencode(params)


def _fetch_page(token: str, query_string: str) -> dict:
    """GET /jobs/?{query_string} and return parsed JSON."""
    import urllib.request, json

    url = f"{BASE_URL}/jobs/?{query_string}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Parsing and storage
# ---------------------------------------------------------------------------

_WORK_MODE_STR = {1: "Remote", 2: "Hybrid", 3: "Onsite"}
_CONTRACT_STR = {1: "Full Time", 2: "Part Time", 3: "Contract", 4: "Internship", 5: "Temporary"}


def _parse_job(job: dict) -> dict | None:
    """Convert raw ScoutBetter API job dict to HireAgent DB row."""
    job_id = job.get("id")
    title = job.get("title") or None

    if not job_id or not title:
        return None

    company = job.get("company") or {}
    company_name = company.get("name") or None

    location = job.get("location") or None
    work_mode_int = job.get("work_mode")
    work_mode_str = _WORK_MODE_STR.get(work_mode_int, "")
    if work_mode_int == 1 and location and "remote" not in location.lower():
        location = f"{location} (Remote)" if location else "Remote"
    elif work_mode_int == 1 and not location:
        location = "Remote"

    # Salary string
    salary = None
    sal_min = job.get("salary_min")
    sal_max = job.get("salary_max")
    if sal_min:
        try:
            lo = int(float(sal_min))
            if sal_max:
                hi = int(float(sal_max))
                salary = f"${lo:,}-${hi:,}/yr"
            else:
                salary = f"${lo:,}/yr"
        except (ValueError, TypeError):
            pass

    # Description from bullets array if present
    bullets = job.get("bullets") or []
    description_parts = []
    if company_name:
        description_parts.append(f"Company: {company_name}")
    contract_int = job.get("contract_type")
    if contract_int:
        description_parts.append(f"Type: {_CONTRACT_STR.get(contract_int, str(contract_int))}")
    if work_mode_str:
        description_parts.append(f"Work mode: {work_mode_str}")
    yoe = job.get("yoe")
    if yoe:
        description_parts.append(f"Experience: {yoe}+ years")
    sub_domain = job.get("sub_domain") or {}
    if sub_domain.get("name"):
        description_parts.append(f"Domain: {sub_domain.get('name')}")
    for b in bullets:
        text = str(b.get("description") or "").strip()
        if text:
            description_parts.append(f"• {text}")

    description = "\n".join(description_parts) if description_parts else None

    # Job URL on ScoutBetter
    url = f"https://scoutbetter.jobs/jobs/{job_id}"
    # The original source job URL (on company site / job board)
    apply_url = job.get("job_url") or None

    return {
        "url": url,
        "title": title,
        "company": company_name,
        "salary": salary,
        "description": description,
        "location": location,
        "site": SITE_LABEL,
        "strategy": "scoutbetter",
        "apply_url": apply_url,
    }


def _store_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    """Insert parsed jobs into DB. Returns (new, existing)."""
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for j in jobs:
        parsed = _parse_job(j)
        if not parsed:
            continue
        try:
            conn.execute(
                "INSERT INTO jobs "
                "(url, title, salary, description, location, site, strategy, discovered_at, application_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    parsed["url"],
                    parsed["title"],
                    parsed["salary"],
                    parsed["description"],
                    parsed["location"],
                    parsed["site"],
                    parsed["strategy"],
                    now,
                    parsed["apply_url"],
                ),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_discovery(cfg: dict | None = None) -> dict:
    """Discover jobs from ScoutBetter and store in the HireAgent DB.

    Reads credentials from env vars SCOUTBETTER_EMAIL / SCOUTBETTER_PASSWORD.
    Reads search config from the 'scoutbetter' key in searches.yaml, or from
    the cfg dict passed directly.

    Returns:
        Dict with new, existing, errors, db_total.
    """
    from hireagent.database import get_connection, init_db

    email = os.environ.get("SCOUTBETTER_EMAIL", "")
    password = os.environ.get("SCOUTBETTER_PASSWORD", "")

    if not email or not password:
        log.error(
            "[scoutbetter] Missing credentials. "
            "Set SCOUTBETTER_EMAIL and SCOUTBETTER_PASSWORD in your .env file."
        )
        return {"new": 0, "existing": 0, "errors": 1, "db_total": 0}

    if cfg is None:
        from hireagent import config as _config
        search_cfg = _config.load_search_config() or {}
        cfg = search_cfg.get("scoutbetter", {})

    max_pages = int(cfg.get("max_pages", 10))

    # Authenticate
    try:
        token = _login(email, password)
    except Exception as e:
        log.error("[scoutbetter] Login failed: %s", e)
        return {"new": 0, "existing": 0, "errors": 1, "db_total": 0}

    init_db()
    conn = get_connection()

    total_new = 0
    total_existing = 0
    errors = 0
    next_url: str | None = None
    page = 0

    while page < max_pages:
        page += 1
        try:
            qs = _build_params(cfg, next_url)
            data = _fetch_page(token, qs)
        except Exception as e:
            log.error("[scoutbetter] Page %d fetch failed: %s", page, e)
            errors += 1
            break

        results = data.get("results") or []
        if not results:
            log.info("[scoutbetter] Page %d: 0 results, stopping", page)
            break

        new, existing = _store_jobs(conn, results)
        total_new += new
        total_existing += existing
        log.info(
            "[scoutbetter] Page %d: %d results → %d new, %d dupes",
            page, len(results), new, existing,
        )

        # Pagination — API returns absolute next URL
        next_url = data.get("next") or None
        if not next_url:
            break

        time.sleep(0.5)  # be polite

    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    log.info(
        "[scoutbetter] Done: %d new | %d dupes | %d errors | %d total in DB",
        total_new, total_existing, errors, db_total,
    )

    return {
        "new": total_new,
        "existing": total_existing,
        "errors": errors,
        "db_total": db_total,
    }
