"""HireAgent database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hireagent.config import DB_PATH

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            eligibility_reason    TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "eligibility_reason": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
}


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL "
        "AND application_url IS NOT NULL AND TRIM(application_url) != '' "
        "AND title IS NOT NULL AND TRIM(title) != '' "
        "AND location IS NOT NULL AND TRIM(location) != '' "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL "
        "AND title IS NOT NULL AND TRIM(title) != '' "
        "AND location IS NOT NULL AND TRIM(location) != '' "
        "AND (apply_status IS NULL OR apply_status IN ('failed'))"
    ).fetchone()[0]

    return stats


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), site, strategy, now),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL",
        "enriched": "full_description IS NOT NULL",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "full_description IS NOT NULL "
            "AND title IS NOT NULL AND TRIM(title) != '' "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND application_url IS NOT NULL "
            "AND title IS NOT NULL AND TRIM(title) != '' "
            "AND location IS NOT NULL AND TRIM(location) != '' "
            "AND (apply_status IS NULL OR apply_status = 'failed')"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


def get_jobs_schema(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Return jobs table schema as a list of dict rows."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
    return [
        {
            "cid": row["cid"],
            "name": row["name"],
            "type": row["type"],
            "notnull": row["notnull"],
            "default": row["dflt_value"],
            "pk": row["pk"],
        }
        for row in rows
    ]


def _existing_job_columns(conn: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}


def list_pending_untailored_jobs(
    conn: sqlite3.Connection | None = None,
    limit: int = 20,
) -> list[dict]:
    """List jobs pending tailoring using schema-safe column selection."""
    if conn is None:
        conn = get_connection()

    cols = _existing_job_columns(conn)
    preferred = [
        "url",
        "title",
        "application_url",
        "fit_score",
        "tailor_attempts",
        "tailored_resume_path",
        "apply_status",
        "apply_error",
        "apply_attempts",
        "last_attempted_at",
    ]
    selected = [c for c in preferred if c in cols]
    if not selected:
        selected = sorted(cols)[:10]

    order_parts = []
    if "fit_score" in cols:
        order_parts.append("COALESCE(fit_score, -1) DESC")
    if "discovered_at" in cols:
        order_parts.append("discovered_at DESC")
    order_sql = f"ORDER BY {', '.join(order_parts)}" if order_parts else ""

    query = (
        f"SELECT {', '.join(selected)} FROM jobs "
        "WHERE full_description IS NOT NULL "
        "AND application_url IS NOT NULL AND TRIM(application_url) != '' "
        "AND title IS NOT NULL AND TRIM(title) != '' "
        "AND location IS NOT NULL AND TRIM(location) != '' "
        "AND tailored_resume_path IS NULL "
        "AND COALESCE(tailor_attempts, 0) < 5 "
        f"{order_sql} LIMIT ?"
    )
    rows = conn.execute(query, (limit,)).fetchall()
    return [dict(row) for row in rows]


def list_recent_apply_failures(
    conn: sqlite3.Connection | None = None,
    limit: int = 20,
) -> list[dict]:
    """List recent apply failures using schema-safe column selection."""
    if conn is None:
        conn = get_connection()

    cols = _existing_job_columns(conn)
    preferred = [
        "url",
        "title",
        "application_url",
        "fit_score",
        "tailored_resume_path",
        "apply_status",
        "apply_error",
        "eligibility_reason",
        "apply_attempts",
        "last_attempted_at",
        "site",
    ]
    selected = [c for c in preferred if c in cols]
    if not selected:
        selected = sorted(cols)[:10]

    order_parts = []
    if "last_attempted_at" in cols:
        order_parts.append("last_attempted_at DESC")
    if "discovered_at" in cols:
        order_parts.append("discovered_at DESC")
    order_sql = f"ORDER BY {', '.join(order_parts)}" if order_parts else ""

    query = (
        f"SELECT {', '.join(selected)} FROM jobs "
        "WHERE (apply_status = 'failed') "
        "OR (apply_error IS NOT NULL AND apply_error != '') "
        f"{order_sql} LIMIT ?"
    )
    rows = conn.execute(query, (limit,)).fetchall()
    return [dict(row) for row in rows]


def list_pending_apply_jobs(
    conn: sqlite3.Connection | None = None,
    limit: int = 20,
) -> list[dict]:
    """List jobs that are currently apply-eligible under strict policy."""
    if conn is None:
        conn = get_connection()

    from hireagent.config import get_targeting_policy
    from hireagent.eligibility import (
        classify_job_data_quality,
        classify_job_eligibility,
        format_eligibility_reasons,
    )

    policy = get_targeting_policy()
    rows = conn.execute(
        """
        SELECT url, title, site, location, application_url, fit_score,
               tailored_resume_path, apply_status, apply_error, apply_attempts,
               last_attempted_at, full_description, eligibility_reason
        FROM jobs
        WHERE tailored_resume_path IS NOT NULL
          AND applied_at IS NULL
          AND (apply_status IS NULL OR apply_status IN ('failed', 'skipped_preflight'))
        ORDER BY COALESCE(fit_score, -1) DESC, discovered_at DESC
        """
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        job = dict(row)
        quality_ok, quality_reason = classify_job_data_quality(job)
        eligibility = classify_job_eligibility(job, policy=policy)
        if not quality_ok or not eligibility["final_eligible"]:
            continue
        job["eligibility_reason"] = job.get("eligibility_reason") or format_eligibility_reasons(eligibility)
        out.append(job)
        if len(out) >= limit:
            break
    return out


def reclassify_eligibility_backfill(conn: sqlite3.Connection | None = None) -> dict:
    """Reclassify all jobs with current strict policy and write skip statuses."""
    if conn is None:
        conn = get_connection()

    from hireagent.config import get_targeting_policy
    from hireagent.eligibility import (
        classify_job_data_quality,
        classify_job_eligibility,
        format_eligibility_reasons,
    )

    policy = get_targeting_policy()
    rows = conn.execute(
        """
        SELECT url, title, site, location, application_url, fit_score,
               full_description, tailored_resume_path, apply_status,
               apply_error, applied_at
        FROM jobs
        """
    ).fetchall()

    stats = {
        "total": len(rows),
        "applied_preserved": 0,
        "skipped_policy": 0,
        "skipped_bad_data": 0,
        "reopened_eligible": 0,
        "unchanged_eligible": 0,
    }

    for row in rows:
        job = dict(row)
        url = job["url"]
        status = (job.get("apply_status") or "").strip().lower()
        if job.get("applied_at") or status == "applied":
            stats["applied_preserved"] += 1
            continue

        quality_ok, quality_reason = classify_job_data_quality(job)
        eligibility = classify_job_eligibility(job, policy=policy)
        eligibility_reason = format_eligibility_reasons(eligibility)

        if not quality_ok:
            conn.execute(
                """
                UPDATE jobs
                SET apply_status='skipped_bad_data',
                    apply_error=?,
                    eligibility_reason=?,
                    agent_id=NULL
                WHERE url=?
                """,
                (quality_reason, quality_reason, url),
            )
            stats["skipped_bad_data"] += 1
            continue

        if not eligibility["final_eligible"]:
            reason = f"policy_skip:{eligibility_reason}"
            conn.execute(
                """
                UPDATE jobs
                SET apply_status='skipped_policy',
                    apply_error=?,
                    eligibility_reason=?,
                    agent_id=NULL
                WHERE url=?
                """,
                (reason, eligibility_reason, url),
            )
            stats["skipped_policy"] += 1
            continue

        if status in {"skipped_policy", "skipped_bad_data"}:
            conn.execute(
                """
                UPDATE jobs
                SET apply_status=NULL,
                    apply_error=NULL,
                    eligibility_reason=?
                WHERE url=?
                """,
                (eligibility_reason, url),
            )
            stats["reopened_eligible"] += 1
        else:
            conn.execute(
                "UPDATE jobs SET eligibility_reason=? WHERE url=?",
                (eligibility_reason, url),
            )
            stats["unchanged_eligible"] += 1

    conn.commit()
    return stats


def reset_tailor_attempts_backfill(
    conn: sqlite3.Connection | None = None,
    eligible_only: bool = True,
) -> dict:
    """Reset tailor attempts for eligible jobs without wiping the DB."""
    if conn is None:
        conn = get_connection()

    from hireagent.config import get_targeting_policy
    from hireagent.eligibility import (
        classify_job_data_quality,
        classify_job_eligibility,
        format_eligibility_reasons,
    )

    policy = get_targeting_policy()
    rows = conn.execute(
        """
        SELECT url, title, site, location, application_url, fit_score,
               full_description, tailored_resume_path, apply_status,
               apply_error, applied_at
        FROM jobs
        WHERE applied_at IS NULL
          AND tailored_resume_path IS NULL
        """
    ).fetchall()

    stats = {
        "total": len(rows),
        "eligible_checked": 0,
        "reset": 0,
        "skipped_ineligible": 0,
        "skipped_bad_data": 0,
    }

    for row in rows:
        job = dict(row)
        url = job["url"]

        if eligible_only:
            stats["eligible_checked"] += 1
            quality_ok, quality_reason = classify_job_data_quality(job)
            if not quality_ok:
                stats["skipped_bad_data"] += 1
                continue
            eligibility = classify_job_eligibility(job, policy=policy)
            if not eligibility["final_eligible"]:
                stats["skipped_ineligible"] += 1
                continue
            eligibility_reason = format_eligibility_reasons(eligibility)
        else:
            eligibility_reason = job.get("eligibility_reason") or "unknown"

        status = (job.get("apply_status") or "").strip().lower()
        clear_status = status in {"skipped_policy", "skipped_bad_data"}

        conn.execute(
            """
            UPDATE jobs
            SET tailor_attempts=0,
                apply_status=CASE WHEN ? THEN NULL ELSE apply_status END,
                apply_error=CASE WHEN ? THEN NULL ELSE apply_error END,
                eligibility_reason=?
            WHERE url=?
            """,
            (1 if clear_status else 0, 1 if clear_status else 0, eligibility_reason, url),
        )
        stats["reset"] += 1

    conn.commit()
    return stats


def reset_stale_in_progress(
    conn: sqlite3.Connection | None = None,
    stale_hours: int = 6,
) -> dict:
    """Reset stale in_progress jobs older than threshold to failed for retry."""
    if conn is None:
        conn = get_connection()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=stale_hours)
    rows = conn.execute(
        """
        SELECT url, title, last_attempted_at, applied_at, apply_status
        FROM jobs
        WHERE apply_status='in_progress'
        """
    ).fetchall()

    reset_urls: list[str] = []
    for row in rows:
        applied_at = row["applied_at"]
        if applied_at:
            continue
        last_attempted = row["last_attempted_at"]
        is_stale = False
        if not last_attempted:
            is_stale = True
        else:
            try:
                dt = datetime.fromisoformat(last_attempted)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                is_stale = dt <= cutoff
            except Exception:
                is_stale = True
        if is_stale:
            reset_urls.append(row["url"])

    for url in reset_urls:
        conn.execute(
            """
            UPDATE jobs
            SET apply_status='failed',
                apply_error=?,
                apply_attempts=COALESCE(apply_attempts, 0) + 1,
                agent_id=NULL
            WHERE url=?
            """,
            (f"stale_in_progress_reset_after_{stale_hours}h", url),
        )

    conn.commit()
    return {
        "stale_hours": stale_hours,
        "checked": len(rows),
        "reset": len(reset_urls),
        "urls": reset_urls,
    }
