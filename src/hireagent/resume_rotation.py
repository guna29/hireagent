"""Resume rotation logic for HireAgent apply pipeline.

Maintains ONE active resume: gunakarthik_naidu_lanka_resume.pdf

Workflow:
- Every eligible job is applied to regardless of fit score.
- If the current job's fit_score >= REUSE_THRESHOLD (9): reuse the active resume as-is.
- If the current job's fit_score < REUSE_THRESHOLD:
    1. Archive the active resume, renaming it to all the companies it served
       e.g. Google_IBM.pdf
    2. Promote this job's tailored PDF to become the new active resume
    3. Clear the companies tracking list

After every successful application, the company is recorded so the next
archive filename reflects all companies that used that resume version.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from hireagent.config import OUTPUT_RESUME_DIR

log = logging.getLogger(__name__)

ACTIVE_RESUME_NAME = "gunakarthik_naidu_lanka_resume.pdf"
ACTIVE_RESUME_PATH = OUTPUT_RESUME_DIR / ACTIVE_RESUME_NAME
COMPANIES_TRACKING_FILE = OUTPUT_RESUME_DIR / "active_resume_companies.json"

# If fit_score >= this, reuse the current active resume without regenerating
REUSE_THRESHOLD = 9


# ---------------------------------------------------------------------------
# Tracking helpers
# ---------------------------------------------------------------------------

def _load_companies() -> list[str]:
    """Return the companies that used the current active resume."""
    try:
        if COMPANIES_TRACKING_FILE.exists():
            return json.loads(COMPANIES_TRACKING_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_companies(companies: list[str]) -> None:
    OUTPUT_RESUME_DIR.mkdir(parents=True, exist_ok=True)
    COMPANIES_TRACKING_FILE.write_text(json.dumps(companies), encoding="utf-8")


def _safe_name(text: str, max_len: int = 25) -> str:
    return re.sub(r"[^\w\s-]", "", text)[:max_len].strip().replace(" ", "_")


# ---------------------------------------------------------------------------
# Core rotation
# ---------------------------------------------------------------------------

def _archive_active_resume(companies: list[str]) -> Path | None:
    """Rename the active resume to a company-grouped archive file.

    e.g. gunakarthik_naidu_lanka_resume.pdf -> Google_IBM.pdf
    Returns the archive path, or None if nothing was archived.
    """
    if not ACTIVE_RESUME_PATH.exists():
        log.warning("Resume rotation: no active resume to archive.")
        return None
    if not companies:
        log.info("Resume rotation: no companies tracked, skipping archive.")
        return None

    parts = [_safe_name(c) for c in companies if c]
    archive_name = "_".join(parts) + ".pdf"
    archive_path = OUTPUT_RESUME_DIR / archive_name

    try:
        shutil.copy2(ACTIVE_RESUME_PATH, archive_path)
        log.info("Resume rotation: archived -> %s (served %d companies)", archive_name, len(companies))
        return archive_path
    except Exception as e:
        log.error("Resume rotation: archive copy failed: %s", e)
        return None


def _promote_to_active(tailored_pdf: Path) -> bool:
    """Copy this job's tailored PDF to gunakarthik_naidu_lanka_resume.pdf."""
    if not tailored_pdf.exists():
        log.error("Resume rotation: tailored PDF not found at %s", tailored_pdf)
        return False
    try:
        OUTPUT_RESUME_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tailored_pdf, ACTIVE_RESUME_PATH)
        log.info("Resume rotation: promoted %s -> %s", tailored_pdf.name, ACTIVE_RESUME_NAME)
        return True
    except Exception as e:
        log.error("Resume rotation: promote failed: %s", e)
        return False


def prepare_resume_for_job(job: dict) -> Path:
    """Decide which resume to use for this job and rotate if needed.

    Called BEFORE run_job() for each application.

    Returns:
        Path to the active resume (always gunakarthik_naidu_lanka_resume.pdf).
    """
    fit_score = int(job.get("fit_score") or 0)
    company = job.get("company") or job.get("site") or ""
    title = job.get("title") or ""
    tailored_path_str = job.get("tailored_resume_path")
    tailored_pdf = Path(tailored_path_str).with_suffix(".pdf") if tailored_path_str else None

    companies = _load_companies()

    if fit_score >= REUSE_THRESHOLD and ACTIVE_RESUME_PATH.exists():
        # Good match — reuse current resume, no rotation needed
        log.info(
            "Resume rotation: score=%d >= %d, reusing active resume for %s (%s)",
            fit_score, REUSE_THRESHOLD, company, title[:40],
        )
        return ACTIVE_RESUME_PATH

    # Score too low — need a fresher resume tailored for this job
    log.info(
        "Resume rotation: score=%d < %d for %s (%s) — rotating resume",
        fit_score, REUSE_THRESHOLD, company, title[:40],
    )

    # 1. Archive the current resume under the companies it served
    _archive_active_resume(companies)

    # 2. Promote this job's tailored PDF to be the new active resume
    if tailored_pdf and tailored_pdf.exists():
        promoted = _promote_to_active(tailored_pdf)
        if promoted:
            _save_companies([])  # Reset tracking — new resume, no companies yet
            log.info("Resume rotation: new active resume ready for %s", company)
        else:
            log.warning("Resume rotation: promotion failed, active resume unchanged.")
    else:
        log.warning(
            "Resume rotation: no tailored PDF for job %s, active resume unchanged.",
            job.get("url", ""),
        )

    return ACTIVE_RESUME_PATH


def record_application(company: str) -> None:
    """Record that a company was successfully applied to with the current active resume.

    Call this after every successful application so the archive filename
    reflects all companies that used this resume version.
    """
    if not company:
        return
    companies = _load_companies()
    if company not in companies:
        companies.append(company)
        _save_companies(companies)
        log.info("Resume rotation: recorded company '%s' (total this version: %d)", company, len(companies))
