"""HireAgent configuration: paths, platform detection, user data."""

import os
import platform
import shutil
from pathlib import Path

# User data directory — all user-specific files live here
APP_DIR = Path(os.environ.get("HIREAGENT_DIR", Path.home() / ".hireagent"))

# Core paths
DB_PATH = APP_DIR / "hireagent.db"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"
OUTPUT_RESUME_DIR = APP_DIR / "output" / "resumes"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"
REPO_ROOT = PACKAGE_DIR.parent.parent
REPO_ENV_PATH = REPO_ROOT / ".env"


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, TAILORED_DIR, COVER_LETTER_DIR, LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR, OUTPUT_RESUME_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict:
    """Load user profile from ~/.hireagent/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `hireagent init` first."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_search_config() -> dict:
    """Load search configuration from ~/.hireagent/searches.yaml."""
    import yaml
    if not SEARCH_CONFIG_PATH.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8"))
        return {}
    return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.).

    Priority order:
      1) ~/.hireagent/sites.yaml (user override)
      2) package config/sites.yaml (default)
    """
    import yaml
    override = APP_DIR / "sites.yaml"
    if override.exists():
        return yaml.safe_load(override.read_text(encoding="utf-8")) or {}

    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 5,
    "apply_timeout": 300,
    "viewport": "1280x900",
    "strict_one_page": os.environ.get("STRICT_ONE_PAGE", "true").lower() == "true",
    "adaptive_tailoring": os.environ.get("ADAPTIVE_TAILORING", "true").lower() == "true",
    "local_llm_only": os.environ.get("LOCAL_LLM_ONLY", "true").lower() == "true",
    "entry_level_only": os.environ.get("ENTRY_LEVEL_ONLY", "true").lower() == "true",
    "software_roles_only": os.environ.get("SOFTWARE_ROLES_ONLY", "true").lower() == "true",
    "us_only": os.environ.get("US_ONLY", "true").lower() == "true",
    "allow_us_remote_only": os.environ.get("ALLOW_US_REMOTE_ONLY", "true").lower() == "true",
    "apply_preflight_check": os.environ.get("APPLY_PREFLIGHT_CHECK", "true").lower() == "true",
}


def load_env():
    """Resolve env config with priority: repo .env, app .env, then process env."""
    from dotenv import dotenv_values

    repo_vals = dotenv_values(REPO_ENV_PATH) if REPO_ENV_PATH.exists() else {}
    app_vals = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}

    # repo .env has highest precedence, then ~/.hireagent/.env, then process env fallback.
    for key in set(repo_vals) | set(app_vals):
        if key in repo_vals and repo_vals[key] is not None:
            os.environ[key] = str(repo_vals[key])
        elif key in app_vals and app_vals[key] is not None:
            os.environ[key] = str(app_vals[key])


def _resolved_env_value(name: str, default: str = "") -> str:
    """Get an env value using repo/app/process precedence."""
    from dotenv import dotenv_values

    repo_vals = dotenv_values(REPO_ENV_PATH) if REPO_ENV_PATH.exists() else {}
    app_vals = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}

    if name in repo_vals and repo_vals[name] is not None:
        return str(repo_vals[name])
    if name in app_vals and app_vals[name] is not None:
        return str(app_vals[name])
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _resolved_env_value(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def get_targeting_policy() -> dict:
    """Return target-job policy flags used by tailoring/apply stages."""
    return {
        "entry_level_only": _env_bool("ENTRY_LEVEL_ONLY", default=True),
        "software_roles_only": _env_bool("SOFTWARE_ROLES_ONLY", default=True),
        "us_only": _env_bool("US_ONLY", default=True),
        "allow_us_remote_only": _env_bool("ALLOW_US_REMOTE_ONLY", default=True),
        "apply_preflight_check": _env_bool("APPLY_PREFLIGHT_CHECK", default=True),
    }


def get_llm_config() -> dict:
    """Return resolved local LLM configuration."""
    provider = _resolved_env_value("LLM_PROVIDER", "").strip().lower()
    model = _resolved_env_value("LLM_MODEL", "llama3:8b").strip() or "llama3:8b"
    base_url = _resolved_env_value("OLLAMA_BASE_URL", "").strip()
    legacy_url = _resolved_env_value("LLM_URL", "").strip()

    if provider == "ollama" or base_url or legacy_url:
        return {
            "provider": "ollama",
            "model": model,
            "base_url": base_url or legacy_url or "http://localhost:11434",
            "configured": True,
        }

    # Local-first default.
    return {
        "provider": "ollama",
        "model": model,
        "base_url": "http://localhost:11434",
        "configured": True,
    }


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + local LLM (Ollama)
    Tier 3 (Full Auto-Apply):       + Claude Code CLI + Chrome
    """
    load_env()

    llm_cfg = get_llm_config()
    has_llm = llm_cfg.get("provider") == "ollama" and bool(llm_cfg.get("base_url"))
    if not has_llm:
        return 1

    has_claude = shutil.which("claude") is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_claude and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    llm_cfg = get_llm_config()
    if required >= 2 and not (llm_cfg.get("provider") == "ollama" and llm_cfg.get("base_url")):
        missing.append("Local LLM config — set LLM_PROVIDER=ollama and OLLAMA_BASE_URL")
    if required >= 3:
        if not shutil.which("claude"):
            missing.append("Claude Code CLI — install from [bold]https://claude.ai/code[/bold]")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
