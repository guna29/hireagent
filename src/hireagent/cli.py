"""HireAgent CLI — the main entry point."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from hireagent import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="hireagent",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
debug_app = typer.Typer(help="Debug helpers for schema-aware inspection.")
app.add_typer(debug_app, name="debug")
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf", "apply")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from hireagent.config import load_env, ensure_dirs
    from hireagent.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]hireagent[/bold] {__version__}")
        raise typer.Exit()


def _render_jobs_schema(schema_rows: list[dict]) -> None:
    schema_table = Table(title="jobs schema", show_header=True, header_style="bold cyan")
    schema_table.add_column("Column")
    schema_table.add_column("Type")
    for row in schema_rows:
        schema_table.add_row(str(row.get("name", "")), str(row.get("type", "")))
    console.print(schema_table)


def _render_row_table(rows: list[dict], title: str) -> None:
    if not rows:
        console.print(f"[yellow]{title}: 0 rows[/yellow]")
        return

    columns = list(rows[0].keys())
    table = Table(title=f"{title} ({len(rows)} rows)", show_header=True, header_style="bold magenta")
    for col in columns:
        table.add_column(col)

    for row in rows:
        rendered = []
        for col in columns:
            val = row.get(col)
            if val is None:
                rendered.append("-")
            else:
                text = str(val)
                rendered.append(text if len(text) <= 120 else text[:117] + "...")
        table.add_row(*rendered)
    console.print(table)


def _db_counts(db_path: Path) -> dict:
    """Return basic jobs counts without mutating DB state."""
    if not db_path.exists():
        return {"jobs": 0, "tailored": 0, "applied": 0}

    conn = sqlite3.connect(str(db_path))
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if not table_exists:
            return {"jobs": 0, "tailored": 0, "applied": 0}

        jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        tailored = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND TRIM(tailored_resume_path) != ''"
        ).fetchone()[0]
        applied = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL OR apply_status = 'applied'"
        ).fetchone()[0]
        return {"jobs": int(jobs), "tailored": int(tailored), "applied": int(applied)}
    finally:
        conn.close()


def _backup_db_file(db_path: Path, backup_dir: Path) -> Path:
    """Create a timestamped SQLite backup using sqlite backup API."""
    if not db_path.exists():
        raise FileNotFoundError(f"DB file not found: {db_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"hireagent_{ts}.db"

    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(backup_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    return backup_path


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """HireAgent — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from hireagent.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(
        7,
        "--min-score",
        help="Minimum fit score for cover stage. Tailor stage now uses software-domain gating.",
    ),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf, apply. Use 'all' to run everything including apply."""
    _bootstrap()

    from hireagent.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2, apply behind Tier 3
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from hireagent.config import check_tier
        check_tier(2, "AI scoring/tailoring")
    if "apply" in stage_list or "all" in stage_list:
        from hireagent.config import check_tier
        check_tier(3, "auto-apply")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
    greenhouse_only: bool = typer.Option(False, "--greenhouse-only", help="Only apply to Greenhouse ATS jobs (greenhouse.io)."),
    no_stealth: bool = typer.Option(False, "--no-stealth", help="Skip stealth gaps between jobs (fast mode for testing)."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from hireagent.config import check_tier, PROFILE_PATH as _profile_path
    from hireagent.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from hireagent.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from hireagent.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from hireagent.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]hireagent init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        from hireagent.database import list_pending_apply_jobs
        conn = get_connection()
        ready = len(list_pending_apply_jobs(conn=conn, limit=1))
        if ready == 0:
            console.print(
                "[red]No apply-eligible jobs ready.[/red]\n"
                "Run [bold]hireagent debug reclassify-eligibility[/bold] and verify with "
                "[bold]hireagent debug jobs --pending-apply[/bold]."
            )
            raise typer.Exit(code=1)

    if gen:
        from hireagent.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from hireagent.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else 0  # 0 = unlimited continuous

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        greenhouse_only=greenhouse_only,
        no_stealth=no_stealth,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from hireagent.database import get_stats

    stats = get_stats()

    console.print("\n[bold]HireAgent Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from hireagent.view import open_dashboard

    open_dashboard()


@debug_app.command("jobs")
def debug_jobs(
    pending_tailor: bool = typer.Option(
        False,
        "--pending-tailor",
        help="Show jobs pending tailoring (domain-based policy, no hardcoded score threshold).",
    ),
    pending_apply: bool = typer.Option(
        False,
        "--pending-apply",
        help="Show jobs ready for apply (tailored resume exists, not applied).",
    ),
    failed_only: bool = typer.Option(
        False,
        "--failed-only",
        help="Show jobs with apply/tailor failures.",
    ),
    limit: int = typer.Option(20, "--limit", "-l", min=1, max=500, help="Max rows to print."),
) -> None:
    """Inspect jobs safely using actual DB schema (no assumptions like id column)."""
    _bootstrap()
    from hireagent.database import (
        get_connection,
        get_jobs_schema,
        list_pending_apply_jobs,
        list_pending_untailored_jobs,
    )

    conn = get_connection()
    schema_rows = get_jobs_schema(conn=conn)
    if not schema_rows:
        console.print("[red]jobs table not found.[/red]")
        raise typer.Exit(code=1)

    schema_cols = [row["name"] for row in schema_rows]
    _render_jobs_schema(schema_rows)

    if pending_tailor and not pending_apply and not failed_only:
        rows = list_pending_untailored_jobs(conn=conn, limit=limit)
        _render_row_table(rows, "jobs debug")
        return

    if pending_apply and not pending_tailor and not failed_only:
        rows = list_pending_apply_jobs(conn=conn, limit=limit)
        _render_row_table(rows, "pending apply (strict)")
        return

    preferred = [
        "job_id",
        "url",
        "company",
        "site",
        "title",
        "fit_score",
        "tailor_attempts",
        "tailored_resume_path",
        "application_url",
        "apply_status",
        "apply_error",
        "apply_attempts",
        "last_attempted_at",
    ]
    selected_cols = [c for c in preferred if c in schema_cols]
    if not selected_cols:
        selected_cols = schema_cols[: min(10, len(schema_cols))]

    where_clauses: list[str] = []
    if pending_tailor:
        where_clauses.append(
            "full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL "
            "AND COALESCE(tailor_attempts, 0) < 5"
        )
    if pending_apply:
        where_clauses.append(
            "tailored_resume_path IS NOT NULL "
            "AND applied_at IS NULL "
            "AND application_url IS NOT NULL AND TRIM(application_url) != '' "
            "AND title IS NOT NULL AND TRIM(title) != '' "
            "AND location IS NOT NULL AND TRIM(location) != '' "
            "AND (apply_status IS NULL OR apply_status = 'failed')"
        )
    if failed_only:
        where_clauses.append(
            "(COALESCE(tailor_attempts, 0) > 0 AND tailored_resume_path IS NULL) "
            "OR (apply_status = 'failed')"
        )

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    order_parts: list[str] = []
    if "fit_score" in schema_cols:
        order_parts.append("COALESCE(fit_score, -1) DESC")
    if "discovered_at" in schema_cols:
        order_parts.append("discovered_at DESC")
    order_sql = f"ORDER BY {', '.join(order_parts)}" if order_parts else ""

    query = f"SELECT {', '.join(selected_cols)} FROM jobs {where_sql} {order_sql} LIMIT ?"
    rows = conn.execute(query, (limit,)).fetchall()

    _render_row_table([dict(row) for row in rows], "jobs debug")


@debug_app.command("apply-failures")
def debug_apply_failures(
    limit: int = typer.Option(20, "--limit", "-l", min=1, max=500, help="Max rows to print."),
) -> None:
    """Inspect recent apply failures with schema-safe columns."""
    _bootstrap()
    from hireagent.database import get_connection, get_jobs_schema, list_recent_apply_failures

    conn = get_connection()
    schema_rows = get_jobs_schema(conn=conn)
    _render_jobs_schema(schema_rows)
    rows = list_recent_apply_failures(conn=conn, limit=limit)
    _render_row_table(rows, "recent apply failures")


@debug_app.command("db-info")
def debug_db_info() -> None:
    """Show active DB path and row counts."""
    from hireagent.config import load_env, ensure_dirs, DB_PATH

    load_env()
    ensure_dirs()

    db_path = Path(DB_PATH)
    counts = _db_counts(db_path)

    table = Table(title="database info", show_header=True, header_style="bold cyan")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("DB path", str(db_path))
    table.add_row("Exists", "yes" if db_path.exists() else "no")
    table.add_row("Jobs", str(counts["jobs"]))
    table.add_row("Tailored", str(counts["tailored"]))
    table.add_row("Applied", str(counts["applied"]))
    console.print(table)


@debug_app.command("backup-db")
def debug_backup_db() -> None:
    """Create a timestamped backup of the active HireAgent database."""
    from hireagent.config import load_env, ensure_dirs, DB_PATH, APP_DIR
    from hireagent.database import close_connection

    load_env()
    ensure_dirs()

    db_path = Path(DB_PATH)
    backup_dir = Path(APP_DIR) / "backups"
    if not db_path.exists():
        console.print(f"[red]DB file not found:[/red] {db_path}")
        raise typer.Exit(code=1)

    close_connection(db_path=db_path)
    backup_path = _backup_db_file(db_path=db_path, backup_dir=backup_dir)

    table = Table(title="database backup", show_header=True, header_style="bold green")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Source DB", str(db_path))
    table.add_row("Backup path", str(backup_path))
    console.print(table)


@debug_app.command("clear-db")
def debug_clear_db(
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation prompt."),
) -> None:
    """Backup and reset the working DB to a fresh schema."""
    from hireagent.config import load_env, ensure_dirs, DB_PATH, APP_DIR
    from hireagent.database import close_connection, init_db

    load_env()
    ensure_dirs()

    db_path = Path(DB_PATH)
    backup_dir = Path(APP_DIR) / "backups"

    if not yes:
        confirmed = typer.confirm(
            f"Clear working DB at {db_path}? A timestamped backup will be created first."
        )
        if not confirmed:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    backup_path: Path | None = None
    if db_path.exists():
        close_connection(db_path=db_path)
        backup_path = _backup_db_file(db_path=db_path, backup_dir=backup_dir)

    # Remove SQLite DB + sidecars, then reinitialize schema.
    close_connection(db_path=db_path)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()

    init_db(db_path=db_path)
    counts = _db_counts(db_path)

    table = Table(title="database reset", show_header=True, header_style="bold yellow")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("DB path", str(db_path))
    table.add_row("Backup path", str(backup_path) if backup_path else "n/a (db did not exist)")
    table.add_row("Schema reinitialized", "yes")
    table.add_row("Jobs", str(counts["jobs"]))
    table.add_row("Tailored", str(counts["tailored"]))
    table.add_row("Applied", str(counts["applied"]))
    console.print(table)


@debug_app.command("reclassify-eligibility")
def debug_reclassify_eligibility() -> None:
    """Recompute strict eligibility for all jobs and write durable skip statuses."""
    _bootstrap()
    from hireagent.database import get_connection, reclassify_eligibility_backfill

    conn = get_connection()
    stats = reclassify_eligibility_backfill(conn=conn)

    summary = Table(title="eligibility reclassify summary", show_header=True, header_style="bold cyan")
    summary.add_column("Metric")
    summary.add_column("Count", justify="right")
    for key in [
        "total",
        "applied_preserved",
        "skipped_bad_data",
        "skipped_policy",
        "reopened_eligible",
        "unchanged_eligible",
    ]:
        summary.add_row(key, str(stats.get(key, 0)))
    console.print(summary)


@debug_app.command("reset-tailor-attempts")
def debug_reset_tailor_attempts(
    eligible_only: bool = typer.Option(
        True,
        "--eligible-only/--all",
        help="Reset attempts only for eligible jobs (default) or for all untailored jobs.",
    ),
) -> None:
    """Reset tailor_attempts for eligible jobs so they can be retried."""
    _bootstrap()
    from hireagent.database import get_connection, reset_tailor_attempts_backfill

    conn = get_connection()
    stats = reset_tailor_attempts_backfill(conn=conn, eligible_only=eligible_only)

    summary = Table(title="reset tailor attempts", show_header=True, header_style="bold cyan")
    summary.add_column("Metric")
    summary.add_column("Count", justify="right")
    for key in [
        "total",
        "eligible_checked",
        "reset",
        "skipped_bad_data",
        "skipped_ineligible",
    ]:
        summary.add_row(key, str(stats.get(key, 0)))
    console.print(summary)


@debug_app.command("reset-stale-in-progress")
def debug_reset_stale_in_progress(
    hours: int = typer.Option(6, "--hours", min=1, max=240, help="Reset in_progress rows older than this many hours."),
) -> None:
    """Reset stale in_progress apply locks to failed with a clear reason."""
    _bootstrap()
    from hireagent.database import get_connection, reset_stale_in_progress

    conn = get_connection()
    result = reset_stale_in_progress(conn=conn, stale_hours=hours)

    summary = Table(title="stale in_progress reset", show_header=True, header_style="bold yellow")
    summary.add_column("Metric")
    summary.add_column("Value", justify="right")
    summary.add_row("stale_hours", str(result.get("stale_hours")))
    summary.add_row("checked", str(result.get("checked")))
    summary.add_row("reset", str(result.get("reset")))
    console.print(summary)

    reset_urls = result.get("urls", [])
    if reset_urls:
        url_table = Table(title="reset urls", show_header=True, header_style="bold magenta")
        url_table.add_column("url")
        for url in reset_urls:
            text = str(url)
            url_table.add_row(text if len(text) <= 180 else text[:177] + "...")
        console.print(url_table)


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import os, shutil
    from hireagent.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, get_chrome_path, get_llm_config,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'hireagent init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'hireagent init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'hireagent init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # ScoutBetter credentials (optional)
    sb_email = os.environ.get("SCOUTBETTER_EMAIL")
    sb_pass = os.environ.get("SCOUTBETTER_PASSWORD")
    if sb_email and sb_pass:
        results.append(("ScoutBetter", ok_mark, f"Configured ({sb_email})"))
    else:
        results.append(("ScoutBetter", "[dim]optional[/dim]",
                        "Set SCOUTBETTER_EMAIL + SCOUTBETTER_PASSWORD in .env for scoutbetter.jobs"))

    # --- Tier 2 checks ---
    import os
    llm_cfg = get_llm_config()
    if llm_cfg.get("provider") == "ollama":
        results.append(("LLM Provider", ok_mark, "Ollama"))
        results.append(("LLM Model", ok_mark, llm_cfg.get("model", "llama3:8b")))
        results.append(("Ollama URL", ok_mark, llm_cfg.get("base_url", "http://localhost:11434")))
    else:
        results.append(("LLM Provider", fail_mark, "Set LLM_PROVIDER=ollama"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]HireAgent Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from hireagent.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs local Ollama config)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


@app.command()
def bot(
    setup: bool = typer.Option(False, "--setup", help="Auto-detect your Telegram chat ID (run once)."),
    token: Optional[str] = typer.Option(None, "--token", help="Telegram bot token (or set HIREAGENT_TELEGRAM_TOKEN env var)."),
) -> None:
    """Start the Telegram bot for remote control from your phone.

    Setup (one-time):
      1. Message @BotFather on Telegram → /newbot → copy the token
      2. Run: hireagent bot --setup --token YOUR_TOKEN
      3. Send any message to your bot in Telegram
      4. Your chat ID will be saved to ~/.hireagent/telegram.env
      5. Run: source ~/.hireagent/telegram.env && hireagent bot
    """
    import os
    from hireagent.config import load_env, APP_DIR
    load_env()

    # Load token from env if not provided
    bot_token = token or os.environ.get("HIREAGENT_TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("HIREAGENT_TELEGRAM_CHAT_ID", "")

    # Try loading from telegram.env if env vars not set
    telegram_env = APP_DIR / "telegram.env"
    if telegram_env.exists() and (not bot_token or not chat_id):
        for line in telegram_env.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line:
                k, v = line.split("=", 1)
                v = v.strip('"').strip("'")
                if k == "HIREAGENT_TELEGRAM_TOKEN" and not bot_token:
                    bot_token = v
                elif k == "HIREAGENT_TELEGRAM_CHAT_ID" and not chat_id:
                    chat_id = v

    if not bot_token:
        console.print("[red]No Telegram bot token found.[/red]")
        console.print("  1. Message @BotFather on Telegram → /newbot")
        console.print("  2. Run: hireagent bot --setup --token YOUR_TOKEN")
        raise typer.Exit(1)

    from hireagent.telegram_bot import run_setup, run_bot

    if setup:
        run_setup(bot_token)
    else:
        if not chat_id:
            console.print("[red]No chat ID found.[/red]")
            console.print("Run: hireagent bot --setup --token YOUR_TOKEN")
            raise typer.Exit(1)
        run_bot(bot_token, chat_id)


if __name__ == "__main__":
    app()
