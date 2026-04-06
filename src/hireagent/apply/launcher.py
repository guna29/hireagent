"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from rich.console import Console
from rich.live import Live

from hireagent import config
from hireagent.database import get_connection
from hireagent.eligibility import classify_job_data_quality, classify_job_eligibility, format_eligibility_reasons
from hireagent.apply import chrome, dashboard, prompt as prompt_mod
from hireagent.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from hireagent.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

logger = logging.getLogger(__name__)

_ATS_HINTS: list[tuple[str, str]] = [
    ("workday", "workday"),
    ("greenhouse", "greenhouse"),
    ("greenhouse", "grnh.se"),
    ("lever", "lever.co"),
    ("ashby", "ashbyhq"),
    ("smartrecruiters", "smartrecruiters"),
    ("icims", "icims"),
    ("successfactors", "successfactors"),
    ("taleo", "taleo"),
    ("oracle-hcm", "oraclecloud"),
    ("bamboohr", "bamboohr"),
    ("jobvite", "jobvite"),
    ("rippling", "ats.rippling.com"),
    ("linkedin", "linkedin.com"),
]

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from hireagent.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, greenhouse_only: bool = False) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    policy = config.get_targeting_policy()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            rows = conn.execute(
                """
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ?)
                  AND tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status IN ('failed', 'skipped_preflight'))
                LIMIT 10
                """,
                (target_url, target_url),
            ).fetchall()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            params: list = [min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            # Optionally restrict to Greenhouse ATS only (includes grnh.se short URLs)
            greenhouse_clause = (
                "AND (application_url LIKE '%greenhouse.io%' OR application_url LIKE '%grnh.se%')"
                if greenhouse_only else ""
            )
            rows = conn.execute(
                f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND application_url IS NOT NULL AND TRIM(application_url) != ''
                  AND title IS NOT NULL AND TRIM(title) != ''
                  AND site NOT IN ('manual', 'Quora', 'quora')
                  AND (apply_status IS NULL OR apply_status IN ('failed', 'skipped_preflight'))
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND COALESCE(fit_score, 0) >= ?
                  {greenhouse_clause}
                  {site_clause}
                  {url_clauses}
                ORDER BY
                  CASE WHEN application_url LIKE '%ultipro%' OR application_url LIKE '%ukg.com%'
                         OR application_url LIKE '%taleo%' OR application_url LIKE '%icims%'
                         OR application_url LIKE '%successfactors%' THEN 1
                       ELSE 0 END ASC,
                  fit_score DESC,
                  url
                LIMIT 50
                """,
                [config.DEFAULTS["max_apply_attempts"]] + params,
            ).fetchall()

        if not rows:
            conn.rollback()
            return None

        from hireagent.config import is_manual_ats

        for row in rows:
            job = dict(row)
            job_url = job["url"]
            apply_url = job.get("application_url") or job_url
            quality_ok, quality_reason = classify_job_data_quality(job)
            if not quality_ok:
                conn.execute(
                    """
                    UPDATE jobs
                    SET apply_status = 'skipped_bad_data',
                        apply_error = ?,
                        eligibility_reason = ?,
                        apply_attempts = COALESCE(apply_attempts, 0) + 1,
                        agent_id = NULL
                    WHERE url = ?
                    """,
                    (quality_reason, quality_reason, job_url),
                )
                logger.info(
                    "Apply candidate skipped bad data | title=%s | company=%s | location=%s | "
                    "job_url=%s | application_url=%s | reason=%s | decision=skip_bad_data",
                    job.get("title", ""),
                    job.get("site", ""),
                    job.get("location", ""),
                    job_url,
                    apply_url,
                    quality_reason,
                )
                continue

            eligibility = classify_job_eligibility(job, policy=policy)

            if not eligibility["final_eligible"]:
                skip_error = _build_policy_skip_error(eligibility)
                conn.execute(
                    """
                    UPDATE jobs
                    SET apply_status = 'skipped_policy',
                        apply_error = ?,
                        eligibility_reason = ?,
                        apply_attempts = COALESCE(apply_attempts, 0) + 1,
                        agent_id = NULL
                    WHERE url = ?
                    """,
                    (skip_error, format_eligibility_reasons(eligibility), job_url),
                )
                logger.info(
                    "Apply candidate excluded by policy | title=%s | company=%s | location=%s | "
                    "job_url=%s | application_url=%s | eligible_entry_level=%s | eligible_us_location=%s | "
                    "eligible_software_role=%s | final_eligible=%s | reason=%s | decision=skip",
                    job.get("title", ""),
                    job.get("site", ""),
                    job.get("location", ""),
                    job_url,
                    apply_url,
                    eligibility["eligible_entry_level"],
                    eligibility["eligible_us_location"],
                    eligibility["eligible_software_role"],
                    eligibility["final_eligible"],
                    format_eligibility_reasons(eligibility),
                )
                continue

            if is_manual_ats(apply_url):
                conn.execute(
                    "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS', apply_attempts = COALESCE(apply_attempts, 0) + 1 WHERE url = ?",
                    (job_url,),
                )
                logger.info(
                    "Apply candidate skipped manual ATS | title=%s | company=%s | location=%s | job_url=%s | application_url=%s",
                    job.get("title", ""),
                    job.get("site", ""),
                    job.get("location", ""),
                    job_url,
                    apply_url,
                )
                continue

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE jobs SET apply_status = 'in_progress',
                               agent_id = ?,
                               last_attempted_at = ?
                WHERE url = ?
                """,
                (f"worker-{worker_id}", now, job_url),
            )
            conn.commit()
            logger.info(
                "Apply candidate selected | title=%s | company=%s | location=%s | job_url=%s | application_url=%s | "
                "eligible_entry_level=%s | eligible_us_location=%s | eligible_software_role=%s | final_eligible=%s | decision=apply",
                job.get("title", ""),
                job.get("site", ""),
                job.get("location", ""),
                job_url,
                apply_url,
                eligibility["eligible_entry_level"],
                eligibility["eligible_us_location"],
                eligibility["eligible_software_role"],
                eligibility["final_eligible"],
            )
            return job

        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()


def _flag_fake_company(job: dict) -> None:
    """Mark ALL jobs from this company as fake_job_ssn and log the flag."""
    conn = get_connection()
    try:
        app_url = job.get("application_url") or job.get("url") or ""
        company_domain = urlparse(app_url).netloc.lower()
        title = job.get("title", "Unknown")
        if not company_domain:
            return
        # Mark every pending/failed job from this domain as permanently flagged
        conn.execute(
            """
            UPDATE jobs
            SET apply_status = 'fake_job_ssn',
                apply_error = 'SSN requested — flagged as fake/scam posting',
                apply_attempts = 99
            WHERE (application_url LIKE ? OR url LIKE ?)
              AND apply_status != 'applied'
            """,
            (f"%{company_domain}%", f"%{company_domain}%"),
        )
        flagged = conn.execute(
            "SELECT changes()"
        ).fetchone()[0]
        conn.commit()
        logger.warning(
            "🚨 FAKE JOB flagged: domain=%s | title=%s | %d job(s) marked",
            company_domain, title, flagged,
        )
    except Exception as e:
        logger.warning("_flag_fake_company error: %s", e)
        conn.rollback()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


def _detect_ats_type(url: str | None) -> str:
    if not url:
        return "unknown"
    lowered = url.lower()
    for ats_name, marker in _ATS_HINTS:
        if marker in lowered:
            return ats_name
    host = urlparse(url).netloc
    return host or "unknown"


def _collect_page_diagnostics(url: str | None, timeout: int = 15) -> dict:
    diagnostics = {
        "url": url or "",
        "final_url": "",
        "ats_type": _detect_ats_type(url),
        "status_code": None,
        "page_loaded": False,
        "page_title": "",
        "form_detected": False,
        "apply_button_detected": False,
        "login_wall_detected": False,
        "captcha_detected": False,
        "blank_page": False,
        "error": "",
    }
    if not url:
        diagnostics["error"] = "missing_url"
        return diagnostics

    try:
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
        )
        with urlopen(req, timeout=timeout) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            final_url = response.geturl() or url
            body_bytes = response.read(400_000)

        html = body_bytes.decode("utf-8", errors="ignore")
        html_lower = html.lower()
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
        page_title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""

        form_detected = "<form" in html_lower
        apply_button_detected = bool(
            re.search(r"<(button|a|input)[^>]{0,300}(apply|submit|start application|easy apply|quick apply)", html_lower)
        )

        diagnostics.update(
            {
                "final_url": final_url,
                "status_code": status_code,
                "page_loaded": bool(status_code and 200 <= int(status_code) < 400),
                "page_title": page_title,
                "form_detected": form_detected,
                "apply_button_detected": apply_button_detected,
                "login_wall_detected": bool(
                    re.search(r"(sign in|log in|single sign-on|sso|workday account|create account)", html_lower)
                ),
                "captcha_detected": any(token in html_lower for token in ("captcha", "recaptcha", "hcaptcha", "turnstile")),
                "blank_page": len(html.strip()) < 120,
            }
        )
    except HTTPError as exc:
        diagnostics["status_code"] = getattr(exc, "code", None)
        diagnostics["final_url"] = exc.geturl() or url
        diagnostics["error"] = f"HTTPError {exc.code}: {exc.reason}"
    except URLError as exc:
        diagnostics["error"] = f"URLError: {exc.reason}"
    except Exception as exc:
        diagnostics["error"] = str(exc)

    return diagnostics


def _derive_no_result_reason(returncode: int | None, diagnostics: dict) -> str:
    if diagnostics.get("error"):
        return "page_probe_error"
    if returncode not in (None, 0):
        return f"claude_exit_{returncode}"
    if not diagnostics.get("page_loaded"):
        return "page_not_loaded"
    if not diagnostics.get("form_detected") and not diagnostics.get("apply_button_detected"):
        return "no_form_or_apply_control_detected"
    return "agent_result_marker_missing"


def _clip(text: str | None, max_len: int = 120) -> str:
    if not text:
        return ""
    text = str(text).strip()
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _detect_failure_flags(output_text: str, apply_url: str, diagnostics: dict, timeout: bool) -> dict:
    lowered = (output_text or "").lower()
    captcha_detected = diagnostics.get("captcha_detected", False) or any(
        token in lowered for token in ("captcha", "recaptcha", "turnstile", "hcaptcha")
    )
    login_wall = diagnostics.get("login_wall_detected", False) or any(
        token in lowered for token in ("login", "sign in", "sso", "account required")
    )
    unsupported_ats = config.is_manual_ats(apply_url) or diagnostics.get("ats_type") in {"unknown"}
    return {
        "captcha_detected": captcha_detected,
        "login_wall": login_wall,
        "unsupported_ats": unsupported_ats,
        "timeout": timeout,
    }


def _build_apply_error(kind: str, reason: str, diagnostics: dict, flags: dict, exception_text: str = "") -> str:
    parts = [
        kind,
        f"reason={reason}",
        f"ats={diagnostics.get('ats_type', 'unknown')}",
        f"loaded={diagnostics.get('page_loaded', False)}",
        f"form={diagnostics.get('form_detected', False)}",
        f"apply_btn={diagnostics.get('apply_button_detected', False)}",
        f"login_wall={flags.get('login_wall', False)}",
        f"captcha={flags.get('captcha_detected', False)}",
        f"unsupported_ats={flags.get('unsupported_ats', False)}",
        f"timeout={flags.get('timeout', False)}",
    ]
    if diagnostics.get("status_code") is not None:
        parts.append(f"http={diagnostics['status_code']}")
    if diagnostics.get("page_title"):
        parts.append(f"title={_clip(diagnostics.get('page_title'), 80)}")
    if diagnostics.get("error"):
        parts.append(f"probe_error={_clip(diagnostics.get('error'), 120)}")
    if exception_text:
        parts.append(f"exception={_clip(exception_text, 120)}")
    return _clip("|".join(parts), 500)


def _log_apply_failure_diagnostics(
    event_type: str,
    worker_id: int,
    job: dict,
    elapsed_seconds: int,
    returncode: int | None,
    diagnostics: dict,
    reason: str,
    flags: dict,
    exception_text: str = "",
) -> None:
    policy = config.get_targeting_policy()
    eligibility = classify_job_eligibility(job, policy=policy)
    job_url = job.get("url") or ""
    apply_url = job.get("application_url") or job_url
    title = job.get("title") or ""
    company = job.get("company") or job.get("site") or ""
    location = job.get("location") or ""
    logger.warning(
        "%s diagnostics | worker=%s | title=%s | company=%s | location=%s | job_url=%s | application_url=%s | "
        "ats=%s | page_title=%s | page_loaded=%s | form_detected=%s | apply_button_detected=%s | "
        "login_wall=%s | captcha=%s | unsupported_ats=%s | timeout=%s | status_code=%s | "
        "eligible_entry_level=%s | eligible_us_location=%s | eligible_software_role=%s | final_eligible=%s | "
        "claude_rc=%s | reason=%s | exception=%s | probe_error=%s",
        event_type,
        worker_id,
        title,
        company,
        location,
        job_url,
        apply_url,
        diagnostics.get("ats_type", "unknown"),
        diagnostics.get("page_title", ""),
        diagnostics.get("page_loaded", False),
        diagnostics.get("form_detected", False),
        diagnostics.get("apply_button_detected", False),
        flags.get("login_wall", False),
        flags.get("captcha_detected", False),
        flags.get("unsupported_ats", False),
        flags.get("timeout", False),
        diagnostics.get("status_code"),
        eligibility.get("eligible_entry_level"),
        eligibility.get("eligible_us_location"),
        eligibility.get("eligible_software_role"),
        eligibility.get("final_eligible"),
        returncode,
        reason,
        exception_text,
        diagnostics.get("error", ""),
    )
    add_event(f"[W{worker_id}] {event_type} ({elapsed_seconds}s) {reason}")


def _build_policy_skip_error(eligibility: dict) -> str:
    return _clip(
        "policy_skip|"
        f"eligible_entry_level={eligibility.get('eligible_entry_level')}|"
        f"eligible_us_location={eligibility.get('eligible_us_location')}|"
        f"eligible_software_role={eligibility.get('eligible_software_role')}|"
        f"reason={format_eligibility_reasons(eligibility)}",
        500,
    )


def _run_apply_preflight(job: dict, enable_probe: bool = True) -> tuple[bool, str, dict]:
    apply_url = job.get("application_url") or job.get("url")
    diagnostics = {
        "ats_type": _detect_ats_type(apply_url),
        "status_code": None,
        "page_loaded": False,
        "page_title": "",
        "form_detected": False,
        "apply_button_detected": False,
        "login_wall_detected": False,
        "captcha_detected": False,
        "blank_page": False,
        "error": "",
    }

    if not apply_url:
        flags = _detect_failure_flags("", "", diagnostics, timeout=False)
        return False, _build_apply_error("preflight", "missing_application_url", diagnostics, flags), diagnostics

    if config.is_manual_ats(apply_url):
        flags = _detect_failure_flags("", apply_url, diagnostics, timeout=False)
        flags["unsupported_ats"] = True
        return False, _build_apply_error("preflight", "preflight_unsupported_ats", diagnostics, flags), diagnostics

    if not enable_probe:
        diagnostics["page_loaded"] = True
        return True, "", diagnostics

    # Many ATS pages (Workday, Greenhouse, Ashby, Lever) contain "sign in" text for optional
    # autofill/account features, causing false-positive login_wall detections in the HTTP probe.
    # Skip the probe for known ATS types and let Chrome handle them directly.
    ats_type = diagnostics.get("ats_type", "unknown")
    _SKIP_PROBE_ATS = {"workday", "greenhouse", "ashby", "lever", "icims", "smartrecruiters", "rippling", "bamboohr", "jobvite", "linkedin"}
    if ats_type in _SKIP_PROBE_ATS:
        diagnostics["page_loaded"] = True
        return True, "", diagnostics

    diagnostics = _collect_page_diagnostics(apply_url)
    flags = _detect_failure_flags("", apply_url, diagnostics, timeout=False)
    ats_type = diagnostics.get("ats_type", "unknown")
    status_code = diagnostics.get("status_code")

    if diagnostics.get("error"):
        # Probe failed with a network/SSL error — let the agent try anyway.
        diagnostics["page_loaded"] = True
        return True, "", diagnostics

    if status_code is not None and int(status_code) >= 400:
        if ats_type == "workday" and int(status_code) == 504:
            reason = "preflight_workday_504"
        else:
            reason = f"preflight_http_{status_code}"
        return False, _build_apply_error("preflight", reason, diagnostics, flags), diagnostics

    # login_wall and captcha_detected are NOT blockers — the agent handles both.
    # Workday login walls are blocked separately via manual_ats config.

    if not diagnostics.get("page_loaded"):
        reason = "preflight_page_not_loaded"
        return False, _build_apply_error("preflight", reason, diagnostics, flags), diagnostics

    if ats_type == "workday" and (
        diagnostics.get("blank_page")
        or (not diagnostics.get("form_detected") and not diagnostics.get("apply_button_detected"))
    ):
        reason = "preflight_workday_blank_or_no_apply_ui"
        return False, _build_apply_error("preflight", reason, diagnostics, flags), diagnostics

    return True, "", diagnostics


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")
    elif not resume_text:
        pdf_path = Path(resume_path).with_suffix(".pdf") if resume_path else None
        if not pdf_path or not pdf_path.exists():
            pdf_path = config.OUTPUT_RESUME_DIR / "gunakarthik_naidu_lanka_resume.pdf"
        if pdf_path and pdf_path.exists():
            try:
                from pypdf import PdfReader as _PdfReader
                _reader = _PdfReader(str(pdf_path))
                resume_text = "\n".join(p.extract_text() or "" for p in _reader.pages)
            except Exception:
                pass

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def _parse_result_from_output(output: str, worker_id: int, elapsed: int,
                              duration_ms: int, job: dict) -> tuple[str, int]:
    """Parse RESULT: markers from agent output and return (status, duration_ms)."""
    def _clean_reason(s: str) -> str:
        return re.sub(r'[*`"]+$', '', s).strip()

    for result_status in ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
        if f"RESULT:{result_status}" in output:
            add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
            update_state(worker_id, status=result_status.lower(),
                         last_action=f"{result_status} ({elapsed}s)")
            return result_status.lower(), duration_ms

    if "RESULT:FAILED" in output:
        for out_line in output.split("\n"):
            if "RESULT:FAILED" in out_line:
                reason = (
                    out_line.split("RESULT:FAILED:")[-1].strip()
                    if ":" in out_line[out_line.index("FAILED") + 6:]
                    else "unknown"
                )
                reason = _clean_reason(reason)
                PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                if reason in PROMOTE_TO_STATUS:
                    add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                    update_state(worker_id, status=reason,
                                 last_action=f"{reason.upper()} ({elapsed}s)")
                    return reason, duration_ms
                add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                update_state(worker_id, status="failed",
                             last_action=f"FAILED: {reason[:25]}")
                return f"failed:{reason}", duration_ms
        return "failed:unknown", duration_ms

    add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
    update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
    return "failed:no_result_line", duration_ms


def _run_job_browser_use(job: dict, port: int, worker_id: int,
                         dry_run: bool, agent_prompt: str,
                         worker_log: Path, log_header: str,
                         start: float, headless: bool = True) -> tuple[str, int]:
    """Run a job application using browser-use + Gemini Flash."""
    import asyncio
    from browser_use import Agent as BrowserAgent
    from browser_use.llm.google import ChatGoogle

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    llm = ChatGoogle(
        model=gemini_model,
        api_key=gemini_key,
    )

    output_parts: list[str] = []
    action_count = 0

    def on_step(agent_state, agent_output, step_num):
        nonlocal action_count
        action_count += 1
        # Log action to worker log
        desc = f"step {step_num}"
        if hasattr(agent_output, 'action') and agent_output.action:
            desc = str(agent_output.action)[:60]
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(f"  >> {desc}\n")
        ws = get_state(worker_id)
        cur_actions = ws.actions if ws else 0
        update_state(worker_id, actions=cur_actions + 1, last_action=desc[:35])

    async def _run():
        from browser_use import Browser, Controller
        browser = Browser(
            headless=headless,
        )

        # --- Custom Gmail IMAP action (no API calls, pure IMAP) ---
        controller = Controller()

        _tg_token = os.environ.get("HIREAGENT_TELEGRAM_TOKEN", "")
        _tg_chat = os.environ.get("HIREAGENT_TELEGRAM_CHAT_ID", "")

        @controller.action(
            "Send a Telegram notification to the user. Use this BEFORE giving up on a job "
            "with RESULT:FAILED or RESULT:EXPIRED so the user knows why you are exiting."
        )
        def notify_user(message: str) -> str:
            if not _tg_token or not _tg_chat:
                return "Telegram not configured."
            try:
                import urllib.request as _ur, json as _json
                body = _json.dumps({"chat_id": _tg_chat, "text": f"⚠️ HireAgent:\n{message}"}).encode()
                req = _ur.Request(
                    f"https://api.telegram.org/bot{_tg_token}/sendMessage",
                    data=body, headers={"Content-Type": "application/json"},
                )
                _ur.urlopen(req, timeout=10)
                return "Notification sent."
            except Exception as e:
                return f"Telegram error: {e}"

        @controller.action(
            "Read latest email from Gmail inbox matching a subject keyword. "
            "Use this when you need a verification code sent to the applicant's email."
        )
        def read_latest_email(subject_keyword: str) -> str:
            import imaplib, email as email_lib, re
            gmail_user = os.environ.get("GMAIL_ADDRESS", "gunakarthiknaidu@gmail.com")
            gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
            if not gmail_pass:
                return "ERROR: GMAIL_APP_PASSWORD not configured."
            try:
                mail = imaplib.IMAP4_SSL("imap.gmail.com")
                mail.login(gmail_user, gmail_pass)
                mail.select("inbox")
                _, ids = mail.search(None, f'SUBJECT "{subject_keyword}"')
                id_list = ids[0].split()
                if not id_list:
                    _, ids2 = mail.search(None, "UNSEEN")
                    id_list = ids2[0].split()
                if not id_list:
                    return "No matching email found. Check the subject keyword."
                latest_id = id_list[-1]
                _, data = mail.fetch(latest_id, "(RFC822)")
                msg = email_lib.message_from_bytes(data[0][1])
                body = ""
                image_parts = []  # (mime_type, bytes)
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body += payload.decode("utf-8", errors="ignore")
                    elif ct == "text/html" and not body:
                        payload = part.get_payload(decode=True)
                        if payload:
                            raw = payload.decode("utf-8", errors="ignore")
                            body += re.sub(r"<[^>]+>", " ", raw)
                    elif ct.startswith("image/"):
                        payload = part.get_payload(decode=True)
                        if payload:
                            image_parts.append((ct, payload))
                mail.logout()

                # OCR any image attachments using Gemini vision
                image_codes: list[str] = []
                if image_parts:
                    try:
                        import google.generativeai as genai
                        import PIL.Image, io
                        genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
                        vision_model = genai.GenerativeModel("gemini-2.5-flash-lite")
                        for mime_type, img_bytes in image_parts[:3]:
                            pil_img = PIL.Image.open(io.BytesIO(img_bytes))
                            resp = vision_model.generate_content([
                                "Extract any verification code, OTP, or numeric code from this image. "
                                "Reply with ONLY the code digits, nothing else. "
                                "If no code found, reply with NONE.",
                                pil_img,
                            ])
                            extracted = resp.text.strip() if resp.text else ""
                            if extracted and extracted.upper() != "NONE":
                                image_codes.append(extracted)
                    except Exception as ocr_err:
                        image_codes.append(f"[image OCR error: {ocr_err}]")

                codes = re.findall(r"\b\d{4,10}\b", body)
                all_codes = codes + image_codes
                summary = body[:1500]
                if all_codes:
                    summary = f"[CODES FOUND: {', '.join(all_codes)}]\n\n" + summary
                return summary
            except Exception as e:
                return f"ERROR reading Gmail: {e}"

        # --- CAPSolver Python action (API call in Python, not browser JS) ---
        capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")

        @controller.action(
            "Solve a CAPTCHA using CAPSolver API. Call this when you detect a CAPTCHA on the page. "
            "Provide captcha_type ('hcaptcha', 'recaptchav2', 'recaptchav3', 'turnstile'), "
            "site_key (the data-sitekey value), and page_url (current page URL). "
            "Returns the solution token to inject."
        )
        def solve_captcha(captcha_type: str, site_key: str, page_url: str) -> str:
            import urllib.request, json, time
            if not capsolver_key:
                return "ERROR: CAPSOLVER_API_KEY not configured."
            try:
                type_map = {
                    "hcaptcha": "HCaptchaTaskProxyless",
                    "recaptchav2": "ReCaptchaV2TaskProxyless",
                    "recaptchav3": "ReCaptchaV3TaskProxyless",
                    "turnstile": "AntiTurnstileTaskProxyless",
                }
                task_type = type_map.get(captcha_type.lower(), "HCaptchaTaskProxyless")
                task_payload: dict = {
                    "type": task_type,
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                }
                if captcha_type.lower() == "recaptchav3":
                    task_payload["minScore"] = 0.3
                    task_payload["pageAction"] = "submit"

                create_body = json.dumps({
                    "clientKey": capsolver_key,
                    "task": task_payload,
                }).encode()
                req = urllib.request.Request(
                    "https://api.capsolver.com/createTask",
                    data=create_body,
                    headers={"Content-Type": "application/json"},
                )
                resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
                if resp.get("errorId", 0) != 0:
                    return f"CAPSolver createTask error: {resp.get('errorDescription', resp)}"
                task_id = resp["taskId"]

                # Poll for result (max 120s)
                for _ in range(24):
                    time.sleep(5)
                    poll_body = json.dumps({"clientKey": capsolver_key, "taskId": task_id}).encode()
                    req2 = urllib.request.Request(
                        "https://api.capsolver.com/getTaskResult",
                        data=poll_body,
                        headers={"Content-Type": "application/json"},
                    )
                    result = json.loads(urllib.request.urlopen(req2, timeout=15).read())
                    if result.get("errorId", 0) != 0:
                        return f"CAPSolver error: {result.get('errorDescription', result)}"
                    if result.get("status") == "ready":
                        sol = result.get("solution", {})
                        token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("userAgent", "")
                        return f"CAPTCHA_TOKEN:{token}"
                return "CAPSolver timeout after 120s."
            except Exception as e:
                return f"CAPSolver exception: {e}"

        @controller.action(
            "Auto-detect and solve any CAPTCHA on the current page using the live browser DOM. "
            "Call this whenever you see an 'I am not a robot' checkbox, hCaptcha, Turnstile, or any CAPTCHA. "
            "No arguments needed — it reads the sitekey directly from the rendered page."
        )
        async def auto_solve_captcha(browser_session) -> str:
            import json, time
            if not capsolver_key:
                return "ERROR: CAPSOLVER_API_KEY not configured."
            try:
                page = await browser_session.get_current_page()
                if not page:
                    return "ERROR: Could not get current browser page."
                # get_url() is the correct method; evaluate() returns JSON-stringified strings
                page_url = await page.get_url()

                # Read sitekey from live rendered DOM (handles JS-injected widgets)
                detect_js = """
() => {
  try {
    // hCaptcha
    const hc = document.querySelector('.h-captcha[data-sitekey], [data-hcaptcha-sitekey], iframe[src*="hcaptcha.com"]');
    if (hc) {
      const sk = hc.getAttribute('data-sitekey') || hc.getAttribute('data-hcaptcha-sitekey');
      if (sk) return {type: 'hcaptcha', sitekey: sk};
    }
    const hcScript = document.querySelector('script[src*="hcaptcha.com"]');
    if (hcScript) {
      const el = document.querySelector('[data-sitekey]');
      if (el && el.getAttribute('data-sitekey').length === 36)
        return {type: 'hcaptcha', sitekey: el.getAttribute('data-sitekey')};
    }
    // Turnstile
    const cf = document.querySelector('.cf-turnstile[data-sitekey], [data-turnstile-sitekey]');
    if (cf) {
      const sk = cf.getAttribute('data-sitekey') || cf.getAttribute('data-turnstile-sitekey');
      if (sk) return {type: 'turnstile', sitekey: sk};
    }
    // reCAPTCHA v3 (render= in script src)
    const rcScript = document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (rcScript) {
      const m = rcScript.src.match(/render=([^&]+)/);
      if (m && m[1] !== 'explicit') return {type: 'recaptchav3', sitekey: m[1]};
    }
    // reCAPTCHA v2
    const rc = document.querySelector('.g-recaptcha[data-sitekey]');
    if (rc) return {type: 'recaptchav2', sitekey: rc.getAttribute('data-sitekey')};
    // Fallback: any data-sitekey
    const any = document.querySelector('[data-sitekey]');
    if (any) {
      const sk = any.getAttribute('data-sitekey');
      const isHcaptcha = !!document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]');
      return {type: isHcaptcha ? 'hcaptcha' : 'recaptchav2', sitekey: sk};
    }
    return null;
  } catch(e) { return null; }
}
"""
                # evaluate() returns a JSON-stringified string — must parse it
                detected_raw = await page.evaluate(detect_js)
                try:
                    detected = json.loads(detected_raw) if detected_raw and detected_raw != "null" else None
                except Exception:
                    detected = None

                if not detected or not isinstance(detected, dict) or not detected.get("sitekey"):
                    return "Could not detect CAPTCHA sitekey from live DOM. Try solve_captcha(captcha_type, site_key, page_url) if you can see the sitekey in the page source."

                captcha_type = detected["type"]
                site_key = detected["sitekey"]

                type_map = {
                    "hcaptcha": "HCaptchaTaskProxyless",
                    "recaptchav2": "ReCaptchaV2TaskProxyless",
                    "recaptchav3": "ReCaptchaV3TaskProxyless",
                    "turnstile": "AntiTurnstileTaskProxyless",
                }
                task_type = type_map.get(captcha_type, "ReCaptchaV2TaskProxyless")
                task_payload: dict = {
                    "type": task_type,
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                }
                if captcha_type == "recaptchav3":
                    task_payload["minScore"] = 0.3
                    task_payload["pageAction"] = "submit"

                import urllib.request, urllib.error
                create_body = json.dumps({"clientKey": capsolver_key, "task": task_payload}).encode()
                req2 = urllib.request.Request("https://api.capsolver.com/createTask",
                    data=create_body, headers={"Content-Type": "application/json"})
                try:
                    raw = urllib.request.urlopen(req2, timeout=15).read()
                    resp = json.loads(raw)
                except urllib.error.HTTPError as he:
                    err_body = he.read().decode("utf-8", errors="replace")
                    try:
                        err_json = json.loads(err_body)
                        err_desc = err_json.get("errorDescription", err_body)
                    except Exception:
                        err_desc = err_body
                    return f"CAPSolver HTTP {he.code}: {err_desc}"
                if resp.get("errorId", 0) != 0:
                    return f"CAPSolver error: {resp.get('errorDescription', resp)}"
                task_id = resp["taskId"]

                for _ in range(24):
                    time.sleep(5)
                    poll = json.dumps({"clientKey": capsolver_key, "taskId": task_id}).encode()
                    req3 = urllib.request.Request("https://api.capsolver.com/getTaskResult",
                        data=poll, headers={"Content-Type": "application/json"})
                    result = json.loads(urllib.request.urlopen(req3, timeout=15).read())
                    if result.get("errorId", 0) != 0:
                        return f"CAPSolver poll error: {result.get('errorDescription', result)}"
                    if result.get("status") == "ready":
                        sol = result.get("solution", {})
                        token = sol.get("gRecaptchaResponse") or sol.get("token") or ""
                        return f"CAPTCHA_SOLVED type={captcha_type} sitekey={site_key} TOKEN:{token}"
                return "CAPSolver timeout after 120s."
            except Exception as e:
                return f"auto_solve_captcha error: {e}"

        @controller.action(
            "Upload the resume PDF to a file input on the current page. "
            "Works for both visible and hidden file inputs (e.g. Greenhouse Dropzone, Ashby, Lever). "
            "Call this instead of upload_file when the resume upload button is a drag-drop area or the upload fails with 'Node is not a file input element'. "
            "No arguments needed — automatically uses the correct resume path."
        )
        async def upload_resume(browser_session) -> str:
            import json as _json
            # Determine which resume to use (prefer master)
            from pathlib import Path as _Path2
            _master = config.OUTPUT_RESUME_DIR / "gunakarthik_naidu_lanka_resume.pdf"
            _worker = _Path2(os.path.expanduser("~/.hireagent/apply-workers/current/Gunakarthik_Naidu_Lanka_Resume.pdf"))
            _resume_file = None
            for _r in [_master, _worker]:
                if _r.exists():
                    _resume_file = str(_r.resolve())
                    break
            if not _resume_file:
                return "ERROR: Resume PDF not found."
            try:
                # Get CDP session from browser_session directly
                cdp_session = await browser_session.get_or_create_cdp_session()
                cdp_client = cdp_session.cdp_client
                session_id = cdp_session.session_id

                # Enable DOM
                await cdp_client.send.DOM.enable(session_id=session_id)
                await cdp_client.send.Runtime.enable(session_id=session_id)

                import asyncio as _asyncio

                async def _try_upload_via_cdp(session_id_inner):
                    """Find file input via CDP and upload using setFileInputFiles."""
                    # Find all file inputs and make them accessible via JS
                    reveal_result = await cdp_client.send.Runtime.evaluate(
                        params={
                            "expression": """
                                (function() {
                                    var inputs = document.querySelectorAll('input[type="file"]');
                                    inputs.forEach(function(inp) {
                                        inp.removeAttribute('disabled');
                                        inp.removeAttribute('readonly');
                                    });
                                    return inputs.length;
                                })()
                            """,
                            "returnByValue": True,
                        },
                        session_id=session_id_inner,
                    )
                    input_count = reveal_result.get("result", {}).get("value", 0)
                    if input_count == 0:
                        return None, "no_input"

                    # Get the document root and query for file input
                    doc = await cdp_client.send.DOM.getDocument(
                        params={"depth": 1},
                        session_id=session_id_inner,
                    )
                    root_node_id = doc["root"]["nodeId"]

                    query_result = await cdp_client.send.DOM.querySelector(
                        params={"nodeId": root_node_id, "selector": 'input[type="file"]'},
                        session_id=session_id_inner,
                    )
                    node_id = query_result.get("nodeId", 0)
                    if not node_id:
                        return None, "no_node_id"

                    describe_result = await cdp_client.send.DOM.describeNode(
                        params={"nodeId": node_id},
                        session_id=session_id_inner,
                    )
                    backend_node_id = describe_result.get("node", {}).get("backendNodeId", 0)
                    if not backend_node_id:
                        return None, "no_backend_node_id"

                    return backend_node_id, "ok"

                # First attempt: try directly
                backend_node_id, status = await _try_upload_via_cdp(session_id)

                if status != "ok":
                    # File input not in DOM yet — try clicking dropzone triggers to activate it
                    trigger_js = """
                    (function() {
                        var selectors = [
                            'button[class*="resume"]', 'button[class*="upload"]', 'button[class*="attach"]',
                            'input[type="file"]', '.dz-clickable', '.dropzone', '[class*="dropzone"]',
                            '[class*="file-upload"]', '[data-qa*="upload"]', '[aria-label*="resume"]',
                            '[aria-label*="upload"]', '[aria-label*="file"]',
                            'label[for*="resume"]', 'label[for*="file"]', 'label[for*="upload"]'
                        ];
                        for (var i = 0; i < selectors.length; i++) {
                            var el = document.querySelector(selectors[i]);
                            if (el && el.tagName !== 'INPUT') { el.click(); return selectors[i]; }
                        }
                        return 'none';
                    })()
                    """
                    await cdp_client.send.Runtime.evaluate(
                        params={"expression": trigger_js, "returnByValue": True},
                        session_id=session_id,
                    )
                    # Wait for file input to appear
                    await _asyncio.sleep(1.5)
                    backend_node_id, status = await _try_upload_via_cdp(session_id)

                if status != "ok":
                    return f"upload_resume: Could not find file input (status={status}). Use upload_file instead."

                # Upload the file via CDP (works on hidden inputs too)
                await cdp_client.send.DOM.setFileInputFiles(
                    params={
                        "files": [_resume_file],
                        "backendNodeId": backend_node_id,
                    },
                    session_id=session_id,
                )
                # Dispatch change/input events so React/Vue components update their state
                import asyncio as _asyncio2
                await _asyncio2.sleep(0.3)
                await cdp_client.send.Runtime.evaluate(
                    params={
                        "expression": """
                            (function() {
                                var input = document.querySelector('input[type="file"]');
                                if (input) {
                                    var ev1 = new Event('input', {bubbles: true});
                                    var ev2 = new Event('change', {bubbles: true});
                                    input.dispatchEvent(ev1);
                                    input.dispatchEvent(ev2);
                                    return 'dispatched';
                                }
                                return 'no_input';
                            })()
                        """,
                        "returnByValue": True,
                    },
                    session_id=session_id,
                )
                return f"Resume uploaded successfully: {_resume_file}"
            except Exception as _e:
                return f"upload_resume error: {_e}"

        # Build list of file paths the agent is allowed to upload
        # Always prefer the master resume (Gunakarthik_Naidu_Lanka_Resume.pdf)
        available_files: list[str] = []
        from pathlib import Path as _Path
        # Check both possible master resume locations
        master_resume = config.OUTPUT_RESUME_DIR / "gunakarthik_naidu_lanka_resume.pdf"
        worker_resume = _Path(os.path.expanduser("~/.hireagent/apply-workers/current/Gunakarthik_Naidu_Lanka_Resume.pdf"))
        for mr in [master_resume, worker_resume]:
            if mr.exists() and str(mr.resolve()) not in available_files:
                available_files.append(str(mr.resolve()))
        # Also add the job-specific tailored path as fallback
        resume_path = job.get("tailored_resume_path")
        if resume_path:
            pdf = _Path(resume_path).with_suffix(".pdf")
            if pdf.exists() and str(pdf.resolve()) not in available_files:
                available_files.append(str(pdf.resolve()))
        cl_path = job.get("cover_letter_path")
        if cl_path:
            cl = _Path(cl_path)
            if cl.exists():
                available_files.append(str(cl.resolve()))

        agent = BrowserAgent(
            task=agent_prompt,
            llm=llm,
            browser_session=browser,
            controller=controller,
            register_new_step_callback=on_step,
            max_failures=8,
            use_vision=True,
            max_history_items=8,   # prune old screenshots — caps token snowball
            available_file_paths=available_files if available_files else None,
        )
        result = await agent.run(max_steps=80)
        return result

    with open(worker_log, "a", encoding="utf-8") as lf:
        lf.write(log_header)
        lf.write("[browser-use + Gemini Flash]\n")

    try:
        result = asyncio.run(_run())

        # Scan ALL history steps for RESULT: markers — more reliable than final_result()
        # because Gemini sometimes puts the result in a non-done step
        all_texts: list[str] = []

        if hasattr(result, 'final_result') and callable(result.final_result):
            fr = result.final_result() or ""
            if fr:
                all_texts.append(fr)

        # Scan each step's extracted_content for RESULT: markers
        if hasattr(result, 'history'):
            for step in (result.history or []):
                for step_result in (getattr(step, 'result', None) or []):
                    content = getattr(step_result, 'extracted_content', None) or ""
                    if content:
                        all_texts.append(content)

        final_text = "\n".join(all_texts)

        # If no RESULT: found anywhere, agent ran out of steps → treat as failed
        if "RESULT:" not in final_text:
            final_text += "\nRESULT:FAILED:no_result_from_agent"

        output_parts.append(final_text)
        output = "\n".join(output_parts)

        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(f"\n[FINAL]\n{final_text}\n")

    except Exception as e:
        output = f"RESULT:FAILED:browser_use_error_{str(e)[:80]}"
        logger.error("browser-use error for %s: %s", job.get("title"), e)

    elapsed = int(time.time() - start)
    duration_ms = int((time.time() - start) * 1000)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_log = config.LOG_DIR / f"gemini_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
    job_log.write_text(output, encoding="utf-8")

    # Handle human escalation
    human_question: str | None = None
    for line in output.split("\n"):
        if "HUMAN_INPUT_NEEDED:" in line:
            human_question = line.split("HUMAN_INPUT_NEEDED:", 1)[-1].strip()
            break

    if human_question and "RESULT:PAUSED" in output:
        add_event(f"[W{worker_id}] PAUSED — asking you on Telegram...")
        update_state(worker_id, status="paused", last_action="waiting for your reply")
        from hireagent.telegram_bot import wait_for_reply, notify
        tg_msg = (
            f"🤖 *Agent needs your help*\n\n"
            f"*Job:* {job.get('title', '')} @ {job.get('site', '')}\n\n"
            f"*Question:* {human_question}\n\n"
            f"Reply with your answer and the bot will continue. "
            f"_(10 min timeout — reply /skip to skip this job)_"
        )
        user_reply = wait_for_reply(tg_msg, timeout_seconds=600)
        if not user_reply or user_reply.strip().lower() in ("/skip", "skip"):
            notify(f"⏭️ Skipping: {job.get('title', '')}")
            return "failed:human_skipped", int((time.time() - start) * 1000)
        # Re-run with user's answer appended
        apply_url = job.get("application_url") or job["url"]
        resume_task = (
            f"You are resuming a job application that was paused for human input.\n\n"
            f"Job: {job.get('title', '')} at {job.get('site', '')}\n"
            f"URL: {apply_url}\n\n"
            f"The previous agent asked: {human_question}\n"
            f"The user replied: {user_reply}\n\n"
            f"Chrome is already open on the application page. "
            f"Take a screenshot to see the current state, then continue "
            f"filling and submitting the application using the user's answer above.\n\n"
            f"Use the same RESULT codes: RESULT:APPLIED, RESULT:FAILED:reason, etc."
        )
        output = _run_job_browser_use(
            job, port, worker_id, dry_run, resume_task,
            worker_log, "", start
        )[0]
        # output here is actually a status string, not text — just return it
        return output, int((time.time() - start) * 1000)

    return _parse_result_from_output(output, worker_id, elapsed, duration_ms, job)


def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            headless: bool = True) -> tuple[str, int]:
    """Run one job application.

    Uses browser-use + Gemini Flash if GEMINI_API_KEY is set (cheap/free).
    Falls back to Claude CLI subprocess otherwise.

    Returns:
        Tuple of (status_string, duration_ms).
    """
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")
    elif not resume_text:
        # Try to extract text from the PDF on the fly
        pdf_path = Path(resume_path).with_suffix(".pdf") if resume_path else None
        if not pdf_path or not pdf_path.exists():
            # Fall back to master resume
            pdf_path = config.OUTPUT_RESUME_DIR / "gunakarthik_naidu_lanka_resume.pdf"
        if pdf_path and pdf_path.exists():
            try:
                from pypdf import PdfReader as _PdfReader
                _reader = _PdfReader(str(pdf_path))
                resume_text = "\n".join(p.extract_text() or "" for p in _reader.pages)
                if txt_path:
                    txt_path.write_text(resume_text, encoding="utf-8")
            except Exception:
                pass

    worker_dir = reset_worker_dir(worker_id)
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()

    # ── FREE path: OpenClaw gateway → Playwright rule-based (zero API cost) ──
    try:
        from hireagent.apply.free_agent import apply_job as _free_apply, RESULT_APPLIED, RESULT_SKIPPED
        from hireagent.config import load_profile as _load_profile
        _profile = _load_profile()
        _resume_pdf = Path(resume_path).with_suffix(".pdf") if resume_path else None
        if not _resume_pdf or not _resume_pdf.exists():
            _resume_pdf = config.OUTPUT_RESUME_DIR / "gunakarthik_naidu_lanka_resume.pdf"

        with open(worker_log, "a", encoding="utf-8") as _lf:
            _lf.write(log_header)
            _lf.write("[free_agent] Starting zero-cost apply\n")

        _status = _free_apply(job, _profile, _resume_pdf, headless=headless)
        duration_ms = int((time.time() - start) * 1000)

        add_event(f"[W{worker_id}] free_agent result: {_status}")
        update_state(worker_id, status="done", last_action="finished")

        return _status, duration_ms
    except Exception as _e:
        duration_ms = int((time.time() - start) * 1000)
        logger.warning("free_agent failed (%s)", _e)
        add_event(f"[W{worker_id}] ERROR: {str(_e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(_e)[:25]}")
        return f"failed:{str(_e)[:100]}", duration_ms

# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
    "no_form_found",           # page has no form or apply button — never retry
    "fake_job_ssn",            # SSN requested — fake/scam posting, never apply
    "job_expired",             # LinkedIn job no longer accepting applications
    "indeed_hosted_not_ats",   # Indeed job listing page, not a real ATS form
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                greenhouse_only: bool = False,
                no_stealth: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id
    policy = config.get_targeting_policy()

    # Release any stale in_progress locks from previous crashed runs
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET apply_status = NULL, agent_id = NULL "
            "WHERE apply_status = 'in_progress'"
        )
        conn.commit()
    except Exception:
        pass

    # Launch Chrome ONCE for the whole worker run so LinkedIn session persists between jobs
    persistent_chrome = None
    if not os.environ.get("GEMINI_API_KEY"):
        add_event(f"[W{worker_id}] Launching Chrome (persistent)...")
        try:
            persistent_chrome = launch_chrome(worker_id, port=port, headless=headless)
        except Exception as _ce:
            logger.warning("Could not pre-launch Chrome: %s", _ce)

    while not _stop_event.is_set():
        if not continuous and limit > 0 and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, greenhouse_only=greenhouse_only)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                try:
                    from hireagent.telegram_bot import notify
                    notify("📭 Queue is empty — no more jobs to apply to right now.\nRun `hireagent run discover enrich score tailor` to add fresh jobs.")
                except Exception:
                    pass
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0
        # Notify user that we're starting this job
        try:
            from hireagent.telegram_bot import notify
            _apply_url = job.get('application_url') or job.get('url', '')
            from hireagent.apply.free_agent import _detect_ats
            _ats = _detect_ats(_apply_url).upper()
            notify(
                f"🚀 *Starting:* {job.get('title', '')}\n"
                f"Score: {job.get('fit_score', 'N/A')}/10 | ATS: {_ats}\n"
                f"{_apply_url}"
            )
        except Exception:
            pass
        eligibility = classify_job_eligibility(job, policy=policy)
        apply_url = job.get("application_url") or job.get("url") or ""
        ats_type = _detect_ats_type(apply_url)

        if not eligibility["final_eligible"]:
            reason = _build_policy_skip_error(eligibility)
            logger.info(
                "Apply policy skip before preflight | title=%s | company=%s | location=%s | job_url=%s | application_url=%s | ats=%s | "
                "eligible_entry_level=%s | eligible_us_location=%s | eligible_software_role=%s | final_eligible=%s | reason=%s | decision=skip",
                job.get("title", ""),
                job.get("site", ""),
                job.get("location", ""),
                job.get("url", ""),
                apply_url,
                ats_type,
                eligibility["eligible_entry_level"],
                eligibility["eligible_us_location"],
                eligibility["eligible_software_role"],
                eligibility["final_eligible"],
                format_eligibility_reasons(eligibility),
            )
            mark_result(job["url"], "skipped_policy", reason, permanent=True)
            failed += 1
            update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed)
            continue

        enable_probe = policy.get("apply_preflight_check", True)
        if dry_run and enable_probe:
            logger.info(
                "Apply preflight disabled for dry_run | title=%s | company=%s | job_url=%s",
                job.get("title", ""),
                job.get("site", ""),
                job.get("url", ""),
            )
            enable_probe = False
        preflight_ok, preflight_error, preflight_diag = _run_apply_preflight(
            job,
            enable_probe=enable_probe,
        )
        if not preflight_ok:
            logger.info(
                "Apply preflight skip | title=%s | company=%s | location=%s | job_url=%s | application_url=%s | ats=%s | "
                "eligible_entry_level=%s | eligible_us_location=%s | eligible_software_role=%s | final_eligible=%s | "
                "page_loaded=%s | form_detected=%s | apply_button_detected=%s | login_wall=%s | captcha=%s | status_code=%s | "
                "preflight_result=failed | decision=skip_preflight | reason=%s",
                job.get("title", ""),
                job.get("site", ""),
                job.get("location", ""),
                job.get("url", ""),
                apply_url,
                preflight_diag.get("ats_type", ats_type),
                eligibility["eligible_entry_level"],
                eligibility["eligible_us_location"],
                eligibility["eligible_software_role"],
                eligibility["final_eligible"],
                preflight_diag.get("page_loaded"),
                preflight_diag.get("form_detected"),
                preflight_diag.get("apply_button_detected"),
                preflight_diag.get("login_wall_detected"),
                preflight_diag.get("captcha_detected"),
                preflight_diag.get("status_code"),
                preflight_error,
            )
            mark_result(job["url"], "skipped_preflight", preflight_error, permanent=True)
            add_event(f"[W{worker_id}] PREFLIGHT_SKIP: {_clip(preflight_error, 80)}")
            failed += 1
            update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed)
            continue

        chrome_proc = None
        try:
            logger.info(
                "Apply preflight pass | title=%s | company=%s | location=%s | job_url=%s | application_url=%s | ats=%s | "
                "eligible_entry_level=%s | eligible_us_location=%s | eligible_software_role=%s | final_eligible=%s | "
                "page_loaded=%s | form_detected=%s | apply_button_detected=%s | decision=apply",
                job.get("title", ""),
                job.get("site", ""),
                job.get("location", ""),
                job.get("url", ""),
                apply_url,
                preflight_diag.get("ats_type", ats_type),
                eligibility["eligible_entry_level"],
                eligibility["eligible_us_location"],
                eligibility["eligible_software_role"],
                eligibility["final_eligible"],
                preflight_diag.get("page_loaded"),
                preflight_diag.get("form_detected"),
                preflight_diag.get("apply_button_detected"),
            )
            # Resume rotation: decide whether to reuse the current
            # gunakarthik_naidu_lanka_resume.pdf or promote this job's
            # tailored PDF as the new active resume.
            try:
                from hireagent.resume_rotation import prepare_resume_for_job, REUSE_THRESHOLD
                prepare_resume_for_job(job)
                _score = int(job.get("fit_score") or 0)
                try:
                    from hireagent.telegram_bot import notify
                    if _score >= REUSE_THRESHOLD:
                        notify(
                            f"📄 *Resume:* Reusing current (Score {_score}/10 ≥ {REUSE_THRESHOLD})\n"
                            f"*Job:* {job.get('title', '')}"
                        )
                    else:
                        notify(
                            f"🔄 *Resume:* Rotating — promoting tailored PDF (Score {_score}/10 < {REUSE_THRESHOLD})\n"
                            f"*Job:* {job.get('title', '')}"
                        )
                except Exception:
                    pass
            except Exception as _rot_err:
                logger.warning("Resume rotation error (non-fatal): %s", _rot_err)

            # Reuse the persistent Chrome launched before the loop (session stays alive)
            chrome_proc = None
            if not os.environ.get("GEMINI_API_KEY"):
                if persistent_chrome and persistent_chrome.poll() is None:
                    chrome_proc = persistent_chrome  # already running — reuse it
                else:
                    add_event(f"[W{worker_id}] Chrome died — relaunching...")
                    persistent_chrome = launch_chrome(worker_id, port=port, headless=headless)
                    chrome_proc = persistent_chrome
            else:
                add_event(f"[W{worker_id}] Starting browser-use agent...")

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run,
                                            headless=headless)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "dry_run":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] DRY RUN complete (not submitted): {job['title'][:30]}")
                applied += 1
                update_state(worker_id, jobs_applied=applied, jobs_done=applied + failed)
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
                # Record company so archive filename reflects all jobs this resume served
                try:
                    from hireagent.resume_rotation import record_application
                    record_application(job.get("company") or job.get("site") or "")
                except Exception as _rec_err:
                    logger.warning("Resume rotation record error (non-fatal): %s", _rec_err)
                # Applied notification with screenshot is sent by free_agent._tg()
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                # If SSN was requested, flag every job from this company as fake
                if result == "fake_job_ssn":
                    _flag_fake_company(job)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)
                # Failure notifications with screenshots are sent by free_agent._tg()
                # Only send launcher-level notification for failures not covered by free_agent
                # (e.g. skipped_preflight, account_required with no browser open)
                _covered = {"fake_job_ssn", "captcha", "no_form_found",
                            "account_required", "job_expired"}
                if result not in _covered:
                    try:
                        from hireagent.telegram_bot import notify
                        notify(
                            f"⚠️ *Failed* — {job.get('title', '')} @ {job.get('site', '')}\n"
                            f"*Reason:* {reason[:120]}"
                        )
                    except Exception:
                        pass
                # Continue to next job — do not stop the pipeline

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            pass  # Chrome persists between jobs — killed after the whole loop ends

        jobs_done += 1
        if target_url:
            break

        # ── Stealth Timing: Human-Mimetic Pacing ─────────────────────────────
        if not _stop_event.is_set() and (continuous or limit == 0 or jobs_done < limit):
            import random as _random

            if no_stealth:
                # Fast mode: minimal 5-10 second gap (testing / watching)
                gap_s = _random.randint(5, 10)
                add_event(f"[W{worker_id}] ⚡ Fast mode — {gap_s}s gap")
                _stop_event.wait(timeout=gap_s)
            else:
                # Every 4 applications: "Coffee Break" — deep sleep 45–75 min
                if jobs_done % 4 == 0:
                    coffee_sleep = _random.randint(45 * 60, 75 * 60)
                    coffee_min = coffee_sleep // 60
                    add_event(f"[W{worker_id}] ☕ Coffee break — sleeping {coffee_min}m before next job")
                    update_state(worker_id, status="idle", last_action=f"coffee break ({coffee_min}m)")
                    try:
                        from hireagent.telegram_bot import notify
                        notify(f"☕ HireAgent taking a {coffee_min}m break after {jobs_done} applications. Will resume shortly.")
                    except Exception:
                        pass
                    if _stop_event.wait(timeout=coffee_sleep):
                        break
                else:
                    # Standard gap: 12–22 min + session drift jitter of +/- 3 min
                    base_gap = _random.randint(12 * 60, 22 * 60)
                    jitter = _random.randint(-180, 180)
                    total_gap = max(60, base_gap + jitter)  # never less than 60s
                    gap_min = round(total_gap / 60, 1)
                    add_event(f"[W{worker_id}] ⏱ Waiting {gap_min}m before next application")
                    update_state(worker_id, status="idle", last_action=f"stealth gap ({gap_min}m)")
                    if _stop_event.wait(timeout=total_gap):
                        break
        # ─────────────────────────────────────────────────────────────────────

    # Kill the persistent Chrome now that the loop is done
    if persistent_chrome:
        cleanup_worker(worker_id, persistent_chrome)

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         greenhouse_only: bool = False, no_stealth: bool = False) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    greenhouse_only=greenhouse_only,
                    no_stealth=no_stealth,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            greenhouse_only=greenhouse_only,
                            no_stealth=no_stealth,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
