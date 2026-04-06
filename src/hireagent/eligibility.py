"""Target-role eligibility classification for tailoring/apply stages."""

from __future__ import annotations

import re
from typing import Any

from hireagent.config import get_targeting_policy

ENTRY_POSITIVE_MARKERS = {
    "entry level",
    "entry-level",
    "new grad",
    "new graduate",
    "graduate program",
    "campus",
    "university grad",
    "university graduate",
    "early career",
    "junior",
    "associate software engineer",
    "associate engineer",
    "associate developer",
    "software engineer i",
    "software engineer 1",
    "sde i",
    "sde 1",
    "engineer i",
    "engineer 1",
    "developer i",
    "developer 1",
    "level 1",
    "l1",
    "recent graduate",
    "recent grad",
    "0-1 year",
    "0 to 1 year",
    "0-2 year",
    "less than 1 year",
    "2025 grad",
    "2026 grad",
    "2025 graduate",
    "2026 graduate",
    "2026 university",
    "university 2026",
    "new grad 2025",
    "new grad 2026",
    "jr.",
    "jr ",
}

SENIORITY_BLOCKERS = {
    "senior",
    " sr ",
    "sr.",
    "staff",
    "principal",
    "manager",
    "director",
    "lead",
    "head of",
    "vp",
    "vice president",
    "phd",
    "ph.d",
    "doctorate",
    "doctoral",
    "postdoc",
    "postdoctoral",
    "post-doc",
    "post-doctoral",
    "intern",
    "internship",
    "co-op",
    "coop",
    "co op",
}

SOFTWARE_ROLE_MARKERS = {
    "software engineer",
    "software developer",
    "software development engineer",
    "sde",
    "backend engineer",
    "backend developer",
    "frontend engineer",
    "front-end engineer",
    "frontend developer",
    "front end developer",
    "full stack engineer",
    "full-stack engineer",
    "full stack developer",
    "fullstack engineer",
    "fullstack developer",
    "platform engineer",
    "infrastructure engineer",
    "cloud engineer",
    "cloud software engineer",
    "devops engineer",
    "site reliability engineer",
    "sre",
    "mobile software engineer",
    "mobile engineer",
    "mobile developer",
    "ios engineer",
    "android engineer",
    "ml engineer",
    "ai engineer",
    "machine learning engineer",
    "data engineer",
    "python developer",
    "java developer",
    "javascript developer",
    "typescript developer",
    "python engineer",
    "java engineer",
    "systems engineer",
    "distributed systems engineer",
    "application developer",
    "application engineer",
    "web developer",
    "web engineer",
}

SOFTWARE_CONTEXT_MARKERS = {
    "software",
    "backend",
    "frontend",
    "full stack",
    "full-stack",
    "api",
    "python",
    "java",
    "typescript",
    "javascript",
    "microservices",
    "distributed systems",
    "cloud",
    "kubernetes",
    "docker",
    "platform",
}

ROLE_BLOCKERS = {
    # Non-technical / go-to-market
    "sales engineer",
    "pre-sales",
    "solutions consultant",
    "business analyst",
    "solutions architect",
    "enterprise architect",
    "manager",
    "director",
    "principal engineer",
    "staff engineer",
    "technical recruiter",
    "technical writer",
    "recruiter",
    # Hardware / physical engineering (not CS)
    "hardware engineer",
    "rf engineer",
    "electrical engineer",
    "electronics engineer",
    "power engineer",
    "mechanical engineer",
    "civil engineer",
    "structural engineer",
    "aerospace engineer",
    "chemical engineer",
    "environmental engineer",
    "manufacturing engineer",
    "process engineer",
    "industrial engineer",
    "materials engineer",
    "field engineer",
    "wind turbine",
    "turbine engineer",
    "energy engineer",
    "petroleum engineer",
    "mining engineer",
    "geotechnical engineer",
    # Networking / security (not SWE)
    "network engineer",
    "network administrator",
    "security engineer",
    "penetration tester",
    "cybersecurity engineer",
    # Other
    "game developer",
    "game engineer",
    "audio engineer",
    "video engineer",
    "broadcast engineer",
}

# Roles restricted to veterans or military personnel only
VETERAN_MILITARY_BLOCKERS = {
    "veterans only",
    "veteran only",
    "military only",
    "active duty only",
    "must be a veteran",
    "reserved for veterans",
    "transitioning military",
    "military exclusive",
    "servicemember",
    "service member only",
    "dod only",
    "department of defense only",
    "requires active security clearance",
    "active secret clearance required",
    "active ts/sci",
    "ts/sci clearance required",
    "top secret clearance required",
    "secret clearance required",
    "dod clearance required",
    "must hold active clearance",
    "must have active clearance",
}

NON_US_MARKERS = {
    "united kingdom",
    "uk",
    "england",
    "canada",
    "ontario",
    "toronto",
    "europe",
    "india",
    "germany",
    "france",
    "spain",
    "singapore",
    "australia",
    "mexico",
}

US_MARKERS = {
    "united states",
    "united states of america",
    "usa",
    "u.s.",
    "us remote",
    "remote us",
    "remote - us",
    "remote (us)",
    "us only",
    "within the us",
}

US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "dc", "de", "fl", "ga", "hi", "ia", "id",
    "il", "in", "ks", "ky", "la", "ma", "md", "me", "mi", "mn", "mo", "ms", "mt", "nc",
    "nd", "ne", "nh", "nj", "nm", "nv", "ny", "oh", "ok", "or", "pa", "ri", "sc", "sd",
    "tn", "tx", "ut", "va", "vt", "wa", "wi", "wv", "wy",
}

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming",
}
# Matches experience requirements like "2+ years", "3 years", "2-5 years", "minimum 2 years"
# The (?<![0-9\-]) lookbehind on the last pattern prevents matching the upper bound of ranges
# like "0-2 years" (would otherwise see "2 years" and incorrectly block it)
_EXP_REQUIREMENT_RE = re.compile(
    r"(\d+)\s*\+?\s*(?:to|-)\s*\d*\s*(?:\+)?\s*years?\s*(?:of\s+)?(?:experience|exp)|"
    r"(\d+)\s*\+\s*years?\s*(?:of\s+)?(?:experience|exp)|"
    r"minimum\s+(?:of\s+)?(\d+)\s*years?\s*(?:of\s+)?(?:experience|exp)|"
    r"at\s+least\s+(\d+)\s*years?\s*(?:of\s+)?(?:experience|exp)|"
    r"(?<![0-9\-])(\d+)\s*years?\s*(?:of\s+)?(?:\w+\s+){0,3}(?:experience|exp)"
)

_TITLE_LEVEL_BLOCKER_RE = re.compile(
    r"\b("
    r"l[4-9]|"
    r"level\s*[4-9]|"
    r"engineer\s*[4-9]|"
    r"[4-9]\s*/\s*[4-9]|"
    r"senior\s+software\s+engineer\s+ii|"
    r"staff\s+engineer|"
    r"principal\s+engineer"
    r")\b"
)
_DESC_ENTRY_MARKERS = {
    "entry level",
    "entry-level",
    "new grad",
    "new graduate",
    "graduate program",
    "university grad",
    "campus",
    "early career",
}


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).lower().split())


def _hits(text: str, tokens: set[str]) -> list[str]:
    return sorted([token for token in tokens if token in text])


def _check_experience_requirement(job: dict) -> tuple[bool, str]:
    """Block jobs that explicitly require more than 1 year of experience."""
    desc = _norm(job.get("full_description"))
    title = _norm(job.get("title"))

    # If title has strong entry-level/new-grad signals, trust that over description text.
    # Companies often write "2 years preferred" in descriptions even for new-grad roles.
    TITLE_ENTRY_OVERRIDES = {
        "new grad", "new graduate", "fresh grad", "entry level", "entry-level",
        "junior", "associate", "university grad", "campus",
    }
    if any(kw in title for kw in TITLE_ENTRY_OVERRIDES):
        return True, "title_signals_entry_level_override"

    # Only scan the description (not title, which rarely has experience numbers)
    for match in _EXP_REQUIREMENT_RE.finditer(desc):
        years_str = next((g for g in match.groups() if g is not None), None)
        if years_str is None:
            continue
        years = int(years_str)
        if 1 < years <= 20:  # >20 is company tenure ("50 years in the industry"), not a job req
            return False, f"requires_{years}_years_experience"

    return True, "experience_ok_0_to_1_years_or_not_specified"


def _is_entry_level(job: dict) -> tuple[bool, str]:
    title = _norm(job.get("title"))

    # Only check seniority blockers against the title, not the description.
    # Job descriptions routinely mention "senior engineers" as team members, which
    # should not disqualify an entry-level role.
    blockers = _hits(f" {title} ", SENIORITY_BLOCKERS)
    if blockers:
        return False, f"seniority_blockers={','.join(blockers[:3])}"

    if _TITLE_LEVEL_BLOCKER_RE.search(title):
        return False, "title_level_blocker_detected"

    # Accept anything that doesn't have seniority/PhD blockers (BS/MS roles)
    return True, "no_seniority_or_phd_blockers"


def _is_us_role(job: dict) -> tuple[bool, str]:
    """Accept US + Remote roles. Block explicitly non-US locations."""
    location = _norm(job.get("location"))

    if not location:
        return True, "location_unknown_accepted"

    # Remote is always OK regardless of company country
    if any(r in location for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        # But block "remote canada", "remote uk", etc.
        non_us_hits = _hits(location, NON_US_MARKERS)
        if non_us_hits:
            # Only block if there's no US signal alongside it
            us_hits = _hits(location, US_MARKERS)
            if not us_hits:
                return False, f"non_us_remote={','.join(non_us_hits[:2])}"
        return True, "remote_accepted"

    # Explicit non-US location markers
    non_us_hits = _hits(location, NON_US_MARKERS)
    if non_us_hits:
        return False, f"non_us_location={','.join(non_us_hits[:2])}"

    # Explicit US signals
    us_hits = _hits(location, US_MARKERS)
    if us_hits:
        return True, f"us_location={','.join(us_hits[:2])}"

    # US state names/codes
    loc_words = set(location.replace(",", " ").split())
    if loc_words & US_STATE_NAMES:
        return True, "us_state_name"
    if loc_words & US_STATE_CODES:
        return True, "us_state_code"

    # If it contains a state name as substring
    if any(state in location for state in US_STATE_NAMES):
        return True, "us_state_in_location"

    # Unknown — accept (let scorer decide; don't over-block)
    return True, "location_unrecognized_accepted"


def _is_veteran_military_restricted(job: dict) -> tuple[bool, str]:
    """Block roles that are explicitly restricted to veterans or require active security clearance."""
    title = _norm(job.get("title"))
    desc = _norm(job.get("full_description"))
    text = f"{title} {desc}"
    hits = _hits(text, VETERAN_MILITARY_BLOCKERS)
    if hits:
        return False, f"veteran_military_restricted={','.join(hits[:3])}"
    return True, "ok"


def _is_software_role(job: dict) -> tuple[bool, str]:
    title = _norm(job.get("title"))
    desc = _norm(job.get("full_description"))
    text = f"{title} {desc}"

    blocker_hits = _hits(title, ROLE_BLOCKERS)
    if blocker_hits:
        # If the title ALSO contains a CS keyword alongside the non-CS role,
        # e.g. "Electrical and Computer Engineering", "Software + Electrical Engineer" → allow it
        CS_RESCUE_KEYWORDS = {
            "software", "computer science", "computer engineering", "computing",
            "data", "ml", "ai", "machine learning", "backend", "frontend",
            "full stack", "fullstack", "cloud", "devops", "platform", "sde", "swe",
        }
        if not any(kw in title for kw in CS_RESCUE_KEYWORDS):
            return False, f"role_blockers={','.join(blocker_hits[:3])}"

    if "performance engineer" in title and not any(
        marker in text for marker in ("software", "backend", "application", "platform")
    ):
        return False, "performance_engineer_without_software_context"

    # If title explicitly says "computer science" or "computer engineering", it's CS — pass immediately
    if any(phrase in title for phrase in ("computer science", "computer engineering", "computing")):
        return True, "cs_degree_field_in_title"

    software_title_hits = _hits(title, SOFTWARE_ROLE_MARKERS)
    if software_title_hits:
        if any(token in title for token in ("ml engineer", "ai engineer", "machine learning engineer")):
            context_hits = _hits(text, SOFTWARE_CONTEXT_MARKERS)
            if len(context_hits) < 2:
                return False, "ai_ml_role_without_software_context"
        return True, f"software_title_markers={','.join(software_title_hits[:3])}"

    # Generic "engineer" in title: ONLY accept if the title itself contains a CS keyword.
    # This prevents "Wind Turbine Engineer", "Electrical Engineer" etc. from sneaking through
    # just because their job description mentions "cloud" or "platform" 3 times.
    CS_TITLE_KEYWORDS = {
        "software", "computer", "computing", "data", "analytics", "ml", "ai",
        "machine learning", "deep learning", "backend", "back end", "back-end",
        "frontend", "front end", "front-end", "full stack", "fullstack", "full-stack",
        "cloud", "devops", "platform", "mobile", "ios", "android", "web", "api",
        "sde", "swe", "automation", "systems", "infrastructure", "site reliability",
    }
    if "engineer" in title:
        # Must have a CS keyword in the title AND 3+ software markers in description
        cs_in_title = any(kw in title for kw in CS_TITLE_KEYWORDS)
        context_hits = _hits(text, SOFTWARE_CONTEXT_MARKERS)
        if cs_in_title and len(context_hits) >= 3:
            return True, f"software_context_markers={','.join(context_hits[:3])}"
        return False, "engineer_title_no_cs_keywords"

    # Accept graduate development programs that have strong software context in description
    if any(kw in title for kw in ("developer", "development program", "graduate program", "apprentice")):
        context_hits = _hits(text, SOFTWARE_CONTEXT_MARKERS)
        if len(context_hits) >= 3:
            return True, f"dev_program_software_context={','.join(context_hits[:3])}"

    return False, "no_software_role_markers"


def classify_job_eligibility(job: dict, policy: dict | None = None) -> dict:
    """Classify a job against entry-level, US, software-role, and veteran/military policy."""
    policy = policy or get_targeting_policy()

    entry_ok, entry_reason = _is_entry_level(job)
    us_ok, us_reason = _is_us_role(job)
    software_ok, software_reason = _is_software_role(job)
    exp_ok, exp_reason = _check_experience_requirement(job)
    veteran_ok, veteran_reason = _is_veteran_military_restricted(job)

    eligible_entry = entry_ok if policy.get("entry_level_only", True) else True
    eligible_us = us_ok if (policy.get("us_only", True) or policy.get("allow_us_remote_only", True)) else True
    eligible_software = software_ok if policy.get("software_roles_only", True) else True

    reasons = []
    if not eligible_entry:
        reasons.append(f"entry:{entry_reason}")
    if not eligible_us:
        reasons.append(f"us:{us_reason}")
    if not eligible_software:
        reasons.append(f"software:{software_reason}")
    if not exp_ok:
        reasons.append(f"experience:{exp_reason}")
    if not veteran_ok:
        reasons.append(f"restricted:{veteran_reason}")

    return {
        "eligible_entry_level": eligible_entry,
        "eligible_us_location": eligible_us,
        "eligible_software_role": eligible_software,
        "eligible_experience": exp_ok,
        "eligible_not_restricted": veteran_ok,
        "final_eligible": eligible_entry and eligible_us and eligible_software and exp_ok and veteran_ok,
        "entry_reason": entry_reason,
        "us_reason": us_reason,
        "software_reason": software_reason,
        "experience_reason": exp_reason,
        "veteran_reason": veteran_reason,
        "reasons": reasons,
    }


def format_eligibility_reasons(result: dict) -> str:
    reasons = result.get("reasons") or []
    if reasons:
        return ";".join(reasons)
    return "eligible"


def classify_job_data_quality(job: dict) -> tuple[bool, str]:
    """Detect malformed job rows that should be skipped before tailor/apply."""
    title = _norm(job.get("title"))
    location = _norm(job.get("location"))
    application_url = _norm(job.get("application_url"))
    site = _norm(job.get("site"))

    if not title:
        return False, "bad_data:blank_title"
    if location in {"remote country", "remote-country", "country remote"}:
        return False, "bad_data:malformed_location"
    if not application_url:
        return False, "bad_data:missing_application_url"
    if title in {site, "unknown", "n/a"}:
        return False, "bad_data:placeholder_title"
    return True, "ok"
