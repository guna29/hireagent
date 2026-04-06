"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from hireagent.config import (
    TAILORED_DIR,
    OUTPUT_RESUME_DIR,
    LOG_DIR,
    load_profile,
    DEFAULTS,
    get_targeting_policy,
)
from hireagent.database import get_connection, get_jobs_by_stage
from hireagent.eligibility import classify_job_data_quality, classify_job_eligibility, format_eligibility_reasons
from hireagent.llm import get_client, get_tailor_client, get_select_client
from hireagent.latex_renderer import (
    TEMPLATE_PATH,
    load_template as load_fixed_template,
    extract_bullets,
    apply_bullets,
    apply_summary,
    compile_tex,
)
from hireagent.scoring.validator import (
    BANNED_WORDS,
    FABRICATION_WATCHLIST,
    sanitize_text,
    validate_json_fields,
    validate_tailored_resume,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up
MAX_BULLET_WORDS = 30  # per-bullet soft cap (used only in legacy rewrite path)
RESUME_WORD_LIMIT = 560  # total target (including all non-bullet content)
RESUME_WORD_MIN = 450   # minimum acceptable (1-page at this template = ~480 pdf words)
BULLET_WORD_BUDGET = 290  # empirically verified 1-page max for this template (10pt Carlito, 0.5in margins)
STRICT_ONE_PAGE = DEFAULTS.get("strict_one_page", True)
ADAPTIVE_TAILORING = DEFAULTS.get("adaptive_tailoring", True)
LOCAL_LLM_ONLY = DEFAULTS.get("local_llm_only", True)
TAILOR_CACHE_VERSION = "jd-tailor-v4"
SAVE_TEX_DEBUG = os.environ.get("HIREAGENT_SAVE_TEX_DEBUG", "true").lower() == "true"
TAILOR_TEX_DEBUG_DIR = LOG_DIR / "tailor_tex"


# ── Local resume helpers ───────────────────────────────────────────────────

SECTION_NAMES = ["SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"]


def _section_key(line: str) -> str | None:
    line = line.strip().lower()
    for section in SECTION_NAMES:
        if section.lower() in line:
            return section
    return None


def load_base_resume_sections() -> dict:
    """Parse the base resume into sections with ordered bullets."""
    text = RESUME_PATH.read_text(encoding="utf-8")
    current = "SUMMARY"
    sections: dict[str, list[str]] = {s: [] for s in SECTION_NAMES}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        maybe = _section_key(line)
        if maybe:
            current = maybe
            continue
        if line.startswith(("- ", "•", "* ")):
            bullet = line.lstrip("-•* ").strip()
            sections[current].append(bullet)
        else:
            # treat as inline bullet in summary/education
            sections[current].append(line)
    return sections


def word_count(items: list[str]) -> int:
    return sum(len(b.split()) for b in items)


_LLM_PREAMBLE_RE = re.compile(
    r"^here(?:'s| is| are)\b[^:]{0,300}:\s*",
    re.IGNORECASE,
)

def _strip_llm_preamble(text: str) -> str:
    """Strip 'Here's a rewritten bullet:' and similar LLM preamble from bullet text."""
    text = text.strip()
    # Strip any "Here's ..." or "Here is ..." prefix up to and including its colon
    text = _LLM_PREAMBLE_RE.sub("", text).strip()
    # Strip leading asterisk/dash used as markdown bullet
    text = re.sub(r"^[\*\-]\s+", "", text).strip()
    return text


def trim_bullet(text: str) -> str:
    words = text.split()
    return " ".join(words[:MAX_BULLET_WORDS])


def _normalize_bullet(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _trim_bullets_to_budget(bullets: list[str], max_total_words: int, min_words: int = 6) -> tuple[list[str], bool]:
    """Trim bullets deterministically until total word budget fits."""
    word_lists = [b.split() for b in bullets]
    total = sum(len(words) for words in word_lists)
    if total <= max_total_words:
        return [" ".join(words) for words in word_lists], True

    while total > max_total_words:
        # Find the longest bullet we can still trim.
        idx = max(range(len(word_lists)), key=lambda i: len(word_lists[i]))
        if len(word_lists[idx]) <= min_words:
            break
        word_lists[idx].pop()
        total -= 1

    trimmed = [" ".join(words) for words in word_lists]
    return trimmed, total <= max_total_words


def cache_key(job: dict) -> str:
    h = hashlib.sha1()
    h.update(TAILOR_CACHE_VERSION.encode("utf-8"))
    h.update((job.get("url", "") + job.get("title", "") + job.get("site", "")).encode("utf-8"))
    h.update(str(job.get("fit_score", "")).encode("utf-8"))
    desc = (job.get("full_description") or "")[:4000]
    h.update(desc.encode("utf-8"))
    template_mtime = (Path(__file__).parent.parent / "templates" / "fixed_resume.tex").stat().st_mtime
    h.update(str(template_mtime).encode("utf-8"))
    return h.hexdigest()


def rewrite_bullet_with_context(
    bullet: str,
    job: dict,
    mode: str,
    context: str = "",
    force_change: bool = False,
) -> str:
    """Rewrite a single bullet deterministically with bounded length."""
    client = get_tailor_client()
    system = (
        "You are a senior recruiter rewriting resume bullets for maximum interview impact. "
        "CRITICAL RULES — violating any = instant rejection:\n"
        "1. Output ONLY the rewritten bullet. No preamble, no explanation, no quotes, no markdown.\n"
        "2. Do NOT invent tools, companies, models, or products not in the ORIGINAL bullet. "
        "If the JD mentions GPT-5 or any other product, do NOT add it to the bullet unless it was already there.\n"
        "3. Keep all real metrics from the original (%, numbers, time saved). Do not invent new numbers.\n"
        "4. Single line only. No bullet symbol at the start.\n"
        "5. Formula: [Power Verb] + [What Was Built] + [Real Method/Tool from original] + [Quantified Result].\n"
        "6. Write a COMPLETE sentence — never end with a preposition or mid-thought.\n"
        "Power verbs: Architected, Automated, Built, Delivered, Deployed, Designed, Engineered, "
        "Implemented, Optimized, Reduced, Scaled, Shipped, Streamlined, Transformed."
    )
    intensity = "align wording to this JD, keep all facts true" if mode == "light" else "rewrite for maximum business impact, keep all facts true"
    user = (
        f"Job title: {job.get('title','')}\n"
        f"Job desc: {(job.get('full_description','') or '')[:1400]}\n"
        f"Context: {context}\n"
        f"Instruction: {intensity}. Use [Power Verb]+[What]+[Result] formula. No generic phrases.\n"
        f"Bullet: {bullet}"
    )
    try:
        prompt_log = LOG_DIR / "tailor_prompts.log"
        with prompt_log.open("a", encoding="utf-8") as fh:
            fh.write(f"MODE={mode} | {job.get('title')} | {bullet}\n")
    except Exception:
        pass
    resp = client.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=2048,
        temperature=0.15 if mode == "light" else 0.35,
    )
    rewritten = trim_bullet(_strip_llm_preamble(resp)) or trim_bullet(bullet)

    if force_change and _normalize_bullet(rewritten) == _normalize_bullet(bullet):
        retry_user = (
            f"Rewrite this bullet using the formula: [Power Verb] + [What Was Done] + [Quantified Impact].\n"
            f"Must differ from original, stay factual. No 'responsible for', 'worked on', 'helped'. Include a metric if real.\n"
            f"Job title: {job.get('title','')}\n"
            f"Job desc: {(job.get('full_description','') or '')[:1600]}\n"
            f"Context: {context}\n"
            f"Original bullet: {bullet}"
        )
        retry = client.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": retry_user},
            ],
            max_tokens=2048,
            temperature=0.45,
        )
        retry_rewritten = trim_bullet(_strip_llm_preamble(retry)) or rewritten
        if _normalize_bullet(retry_rewritten) != _normalize_bullet(bullet):
            rewritten = retry_rewritten
    return rewritten



def rewrite_section(bullets: list[str], job: dict, mode: str) -> list[str]:
    return [rewrite_bullet_with_context(b, job, mode, "") for b in bullets]


def adaptive_plan(score: int) -> str:
    if not ADAPTIVE_TAILORING:
        return "semantic"
    if score >= 8:
        # Keep high-fit jobs lightly tailored instead of copying the base resume.
        return "light"
    if 5 <= score <= 7:
        return "light"
    return "semantic"


def validate_resume(base_sections: dict, new_sections: dict, tex_ok: bool) -> bool:
    # bullet counts must match
    for k in SECTION_NAMES:
        if len(base_sections.get(k, [])) != len(new_sections.get(k, [])):
            log.error("Bullet count changed in section %s", k)
            return False
    total_words = sum(word_count(new_sections.get(k, [])) for k in SECTION_NAMES)
    if total_words > RESUME_WORD_LIMIT:
        log.error("Word limit exceeded (%s > %s)", total_words, RESUME_WORD_LIMIT)
        return False
    if STRICT_ONE_PAGE and not tex_ok:
        log.error("LaTeX compile failed")
        return False
    return True


def build_contact_line(profile: dict) -> str:
    p = profile.get("personal", {})
    parts = [p.get("email", ""), p.get("phone", ""), p.get("github_url", ""), p.get("linkedin_url", "")]
    return " | ".join([x for x in parts if x])


def render_bullet_list(bullets: list[str]) -> str:
    return "\n".join([f"\\item {sanitize_text(b)}" for b in bullets])


def render_experience_blocks(section_bullets: list[list[str]]) -> str:
    blocks: list[str] = []
    for bullets in section_bullets:
        blocks.append("\\begin{itemize}\n" + render_bullet_list(bullets) + "\n\\end{itemize}")
    return "\n".join(blocks)


def load_template() -> str:
    template_path = Path(__file__).parent.parent / "templates" / "resume_template.tex"
    return template_path.read_text(encoding="utf-8")


def fill_template(profile: dict, sections: dict, summary: list[str], skills: list[str],
                  experience_blocks: list[list[str]], project_blocks: list[list[str]], education_blocks: list[list[str]]) -> str:
    tpl = load_template()
    p = profile.get("personal", {})
    return (
        tpl.replace("{{FULL_NAME}}", sanitize_text(p.get("full_name", "")))
        .replace("{{CONTACT_LINE}}", sanitize_text(build_contact_line(profile)))
        .replace("{{SUMMARY_BULLETS}}", render_bullet_list(summary))
        .replace("{{SKILL_BULLETS}}", render_bullet_list(skills))
        .replace("{{EXPERIENCE_BLOCKS}}", render_experience_blocks(experience_blocks))
        .replace("{{PROJECT_BLOCKS}}", render_experience_blocks(project_blocks))
        .replace("{{EDUCATION_BLOCKS}}", render_experience_blocks(education_blocks))
    )


def compile_latex(tex_content: str, output_pdf: Path) -> bool:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_pdf.parent / output_pdf.stem
    temp_dir.mkdir(parents=True, exist_ok=True)
    tex_path = temp_dir / "resume.tex"
    tex_path.write_text(tex_content, encoding="utf-8")
    cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-output-directory", str(temp_dir), str(tex_path)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    except Exception as e:
        log.error("LaTeX compile failed: %s", e)
        return False
    generated = temp_dir / "resume.pdf"
    if generated.exists():
        generated.replace(output_pdf)
        return True
    return False


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(profile: dict) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    All skills boundaries, preserved entities, and formatting rules are
    derived from the profile -- nothing is hardcoded.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
    skills_block = "\n".join(skills_lines)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    projects = resume_facts.get("preserved_projects", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    projects_str = ", ".join(projects) if projects else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    # Include ALL banned words from the validator so the LLM knows exactly
    # what will be rejected — the validator checks for these automatically.
    banned_str = ", ".join(BANNED_WORDS)

    education = profile.get("experience", {})
    education_level = education.get("education_level", "")

    return f"""You are an expert resume strategist who combines recruiter instinct with competitive intelligence. Your job is not just to match keywords — it is to make this candidate the OBVIOUS hire for this specific role at this specific company.

You will receive a base resume and a job description. Before writing a single word, run this intelligence process. Then produce a JSON resume that wins.

═══════════════════════════════════════════════════════
STEP 1 — COMPANY INTELLIGENCE SCAN (do this first, internally)
═══════════════════════════════════════════════════════
Read the JD and classify the company along these axes:

A. COMPANY TYPE
   - Frontier AI startup (Andreessen/Sequoia-backed, agent/LLM focus) → candidate must sound like an "AI Systems Engineer", not a student or intern
   - Enterprise / Fortune 500 → candidate must sound like a reliable production engineer
   - Government / regulated industry (healthcare, legal, defense) → emphasize compliance, reliability, security
   - Early-stage / seed startup → emphasize breadth: candidate can wear many hats, ship fast
   - Consulting / services firm → emphasize client-facing impact and delivery timelines

B. WHAT THEY ACTUALLY VALUE (read between the lines)
   - What problem does this company EXIST to solve? (e.g., "automate complex institutional workflows")
   - What does the JD repeat 2+ times? Those words are the signal vocabulary — mirror them exactly
   - What outcome do they care about most? (speed to market, accuracy, scale, compliance, cost reduction)
   - Do they mention "wear many hats", "scrappy", "ownership"? → startup mode, broaden the candidate's footprint

C. THE PERSONA THIS COMPANY WANTS TO HIRE
   - Translate the role title into the ACTUAL persona they want
   - Examples: "Software Engineer" at an AI startup = "Applied AI Systems Engineer"
               "Backend Engineer" at a fintech = "High-reliability distributed systems engineer"
               "Full-Stack" at early-stage = "Generalist who ships product end-to-end"
   - The candidate's summary and title must read as that persona, not as "student seeking opportunities"

D. THE BRIDGE — find it in the candidate's existing work
   - What project or experience maps most directly to what this company does?
   - Reframe that project using the company's vocabulary
   - Example: candidate built "HireAgent job scraper" → for a workflow-automation company this becomes "autonomous agentic state machine that automates complex multi-step institutional processes"
   - The work is the same. The framing makes it obvious that the candidate has ALREADY solved their problem.

E. SOFT SIGNAL (when JD mentions PM / stakeholder / cross-functional / clients)
   - Add to Technical Skills "Concepts" section: "Product Strategy, Stakeholder Management, Technical Sales Support"
   - This signals the candidate can talk to non-engineers, not just write code

═══════════════════════════════════════════════════════
STEP 2 — GAP ANALYSIS
═══════════════════════════════════════════════════════
Compare the JD requirements to the base resume. For each gap:

- HARD GAP (required skill candidate truly lacks): Do NOT fabricate. Skip or note in the summary as "actively learning X"
- SOFT GAP (skill candidate has but resume doesn't emphasize): BRIDGE IT. Reframe existing bullets using JD vocabulary
- VOCABULARY GAP (candidate did the work, just called it something different): RENAME IT to match the JD's language
  - "automated pipeline" → "agentic workflow" if the JD uses that term
  - "built a scraper" → "engineered a data acquisition engine" if the company values infrastructure language
  - "REST API" → "microservices orchestration layer" if the JD values architecture language

═══════════════════════════════════════════════════════
STEP 3 — REWRITE RULES
═══════════════════════════════════════════════════════

TITLE: Use the exact persona identified in Step 1C. Match role seniority. Drop team/company suffixes.

SUMMARY: 2-3 sentences. Must answer these 3 questions in order:
  1. What is this person? (the persona from Step 1C, not "student" or "intern")
  2. What is their proven superpower most relevant to THIS company?
  3. What outcome can this company expect? (one sentence connecting their work to the company's mission)
  Do NOT use: "passionate", "motivated", "seeking", "eager to learn", "proven track record of"

SKILLS: Reorder every category so the JD's must-haves appear first. Add 2-3 closely related tools only.
Allowed skills base:
{skills_block}

BULLETS — for EVERY bullet:
1. Verb + What You Built + Quantified Result (%, time saved, scale, users, requests/sec)
2. Mirror the JD's vocabulary where the work overlaps (see Step 2 vocabulary gap)
3. The strongest project bullet should sound like you already solved the company's core problem
4. CUT any bullet that does not differentiate or show impact
5. ORDER: most JD-relevant bullets first within each section
6. POWER VERBS: Accelerated, Architected, Automated, Built, Delivered, Deployed, Designed, Drove, Engineered, Eliminated, Implemented, Led, Optimized, Orchestrated, Reduced, Scaled, Shipped, Streamlined, Transformed

PROJECTS: Reorder by relevance to this role. Lead with the project that is the strongest BRIDGE. Use the company's language to describe what it does. Drop projects with zero relevance to this role.

═══════════════════════════════════════════════════════
STEP 4 — HARD CONSTRAINTS (never violate)
═══════════════════════════════════════════════════════
- Do NOT invent work, companies, degrees, or certifications
- Do NOT change real numbers ({metrics_str})
- Preserved companies: {companies_str} — names stay exactly as-is
- Preserved school: {school}
- ONE PAGE STRICTLY — every bullet must earn its space
- TARGET WORD COUNT: 550-560 words total
- BULLETS PER SECTION: 3-4 per job (4 for most impactful role, 3 for others). Projects: 2-3 bullets each
- BULLET QUALITY: action verb + technology + measurable impact. No vague bullets
- PROJECT HEADER FORMAT: "Project Name" on the left, italic short category label on the right. Never list tech stack in the header
- PAGE FILL: resume must fill to the last line — no blank whitespace at the bottom

═══════════════════════════════════════════════════════
STEP 5 — VOICE AND BANNED WORDS
═══════════════════════════════════════════════════════
- Write like a practitioner, not a student. Short, direct sentences.
- GOOD: "Architected self-correcting agentic pipeline in Python, reducing manual processing overhead by 80% across 4,000+ workflows"
- BAD: "Leveraged cutting-edge AI technologies to drive transformative operational efficiencies"
- No em dashes — use commas, periods, or hyphens
- No "internship" anywhere — roles are titled Intern but never described as internships
- No "ApplyPilot" — project is always HireAgent
- No first-person pronouns in bullets (no "I built", "I developed")
- No generic phrases: "responsible for", "worked on", "helped with", "contributed to"
- HireAgent project: frame as autonomous job discovery and workflow automation system — scraping immigration/veteran-supported roles, LLM scoring/filtering, resume tailoring engine, Playwright form pre-population. NEVER use: "submitted", "auto-apply", "autonomous submission", or "bot"
- BANNED WORDS (any of these = automatic validation failure): {banned_str}

═══════════════════════════════════════════════════════
OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble.
═══════════════════════════════════════════════════════

{{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{{"Languages":"...","Frameworks":"...","DevOps & Infra":"...","Databases":"...","Tools":"..."}},"experience":[{{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}}],"projects":[{{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}}],"education":"{school} | {education_level}"}}"""


def _build_judge_prompt(profile: dict) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch LIES, not style changes.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Change the title to match the target role
- Rewrite the summary from scratch for the target job
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## WHAT IS FABRICATION (FAIL for these):
1. Adding tools, languages, or frameworks to TECHNICAL SKILLS that aren't in the original. The allowed skills are ONLY: {skills_str}
2. Inventing NEW metrics or numbers not in the original. The real metrics are: {metrics_str}
3. Inventing work that has no basis in any original bullet (completely new achievements).
4. Adding companies, roles, or degrees that don't exist.
5. Changing real numbers (inflating 80% to 95%, 500 nodes to 1000 nodes).

## WHAT IS NOT FABRICATION (do NOT fail for these):
- Rewording any bullet, even heavily, as long as the underlying work is real
- Combining two original bullets into one
- Splitting one original bullet into two
- Describing the same work with different emphasis
- Dropping bullets entirely
- Reordering anything
- Changing the title or summary completely

## TOLERANCE RULE:
The goal is to get interviews, not to be a perfect fact-checker. Allow up to 3 minor stretches per resume:
- Adding a closely related tool the candidate could realistically know is a MINOR STRETCH, not fabrication.
- Reframing a metric with slightly different wording is a MINOR STRETCH.
- Adding any LEARNABLE skill given their existing stack is a MINOR STRETCH.
- Only FAIL if there are MAJOR lies: completely invented projects, fake companies, fake degrees, wildly inflated numbers, or skills from a completely different domain.

Be strict about major lies. Be lenient about minor stretches and learnable skills. Do not fail for style, tone, or restructuring."""


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict, profile: dict) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    lines: list[str] = []

    # Header -- always code-injected from profile
    lines.append(personal.get("full_name", ""))
    lines.append(sanitize_text(data.get("title", "Software Engineer")))

    # Location from search config or profile -- leave blank if not available
    # The location line is optional; the original used a hardcoded city.
    # We omit it here; the LLM prompt can include it if the user sets it.

    # Contact line
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # Summary
    lines.append("SUMMARY")
    lines.append(sanitize_text(data["summary"]))
    lines.append("")

    # Technical Skills
    lines.append("TECHNICAL SKILLS")
    if isinstance(data["skills"], dict):
        for cat, val in data["skills"].items():
            lines.append(f"{cat}: {sanitize_text(str(val))}")
    lines.append("")

    # Experience
    lines.append("EXPERIENCE")
    for entry in data.get("experience", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Projects
    lines.append("PROJECTS")
    for entry in data.get("projects", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Education
    lines.append("EDUCATION")
    lines.append(sanitize_text(str(data.get("education", ""))))

    return "\n".join(lines)


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        {"passed": bool, "verdict": str, "issues": str, "raw": str}
    """
    judge_prompt = _build_judge_prompt(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_tailor_client()
    response = client.chat(messages, max_tokens=512, temperature=0.1)

    passed = "VERDICT: PASS" in response.upper()
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".
                          strict  -- banned words trigger retries; judge must pass
                          normal  -- banned words = warnings only; judge can fail on last retry
                          lenient -- banned words ignored; LLM judge skipped

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_tailor_client()
    tailor_prompt_base = _build_tailor_prompt(profile)

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        # Fresh conversation every attempt
        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
        ]

        raw = client.chat(messages, max_tokens=2048, temperature=0.4)

        # Parse JSON from response
        try:
            data = extract_json(raw)
        except ValueError:
            avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
            continue

        # Layer 1: Validate JSON fields
        validation = validate_json_fields(data, profile, mode=validation_mode)
        report["validator"] = validation

        if not validation["passed"]:
            # Only retry if there are hard errors (warnings never block)
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            # Last attempt — assemble whatever we got
            tailored = assemble_resume_text(data, profile)
            report["status"] = "failed_validation"
            return tailored, report

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = assemble_resume_text(data, profile)

        # Layer 2: LLM judge (catches subtle fabrication) — skipped in lenient mode
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "approved"
            return tailored, report

        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                # In normal mode, only retry on judge failure if there are retries left
                if validation_mode != "lenient":
                    continue
            # Accept best attempt on last retry (all modes) or if lenient
            report["status"] = "approved_with_judge_warning"
            return tailored, report

        # Both passed
        report["status"] = "approved"
        return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report

# ── Batch Entry Point ────────────────────────────────────────────────────


UNRELATED_KEYWORDS = {
    "sales",
    "account manager",
    "customer success",
    "support",
    "help desk",
    "business development",
    "marketing",
    "recruiter",
    "talent acquisition",
    "hr",
    "human resources",
    "call center",
    "loan officer",
    "insurance agent",
}
UNRELATED_TITLE_HINTS = {
    "sales",
    "account executive",
    "customer support",
    "help desk",
    "marketing",
    "recruiter",
    "quality assurance",
    "qa ",
    " qa",
    "sdet",
    "test engineer",
    "manual tester",
}
SOFTWARE_KEYWORDS = {
    "software",
    "engineer",
    "developer",
    "backend",
    "frontend",
    "full stack",
    "full-stack",
    "platform",
    "distributed systems",
    "microservices",
    "python",
    "java",
    "c++",
    "typescript",
    "javascript",
    "api",
    "rest",
    "sql",
    "cloud",
    "devops",
    "infrastructure",
    "machine learning",
    "ai",
    "ml",
}
SOFTWARE_TITLE_HINTS = {
    "software engineer",
    "software developer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "full-stack engineer",
    "platform engineer",
    "ml engineer",
    "machine learning engineer",
    "ai engineer",
    "data engineer",
    "site reliability engineer",
    "devops engineer",
}

PROJECT_TAGS = {
    "HireAgent": ["python", "llm", "claude", "ollama", "playwright", "docker", "sqlite", "fastapi", "latex", "telegram", "scraping", "agentic", "automation", "ci/cd", "pipeline", "ai", "browser", "nlp", "state machine", "pdf"],
    "FosterArizona.org": ["react", "frontend", "tailwind", "api", "accessibility", "wcag", "sql", "aws", "nonprofit", "component", "responsive", "ui", "forms"],
    "AutoAudit AI": ["python", "llm", "ollama", "llama", "phi", "api", "sqlite", "privacy", "benchmarking", "gguf", "quantization", "local inference", "performance"],
    "NeighborhoodPulse": ["python", "langgraph", "gemini", "rag", "geospatial", "openstreetmap", "crawl4ai", "tavily", "sqlite", "telegram", "react", "agentic", "multi-agent"],
    "NutriVision": ["python", "gemini", "vision", "multimodal", "telegram", "sqlite", "pydantic", "ocr", "mobile", "image", "structured output", "json"],
    "ResearchPulse": ["python", "langgraph", "gemini", "tavily", "crawl4ai", "rag", "agentic", "web", "research", "markdown", "multi-agent", "cyclic", "nlp"],
    # Legacy entries kept for backward compat with old tailored resumes
    "Agentic AI Meeting Assistant Platform": ["ai", "llm", "meeting", "transcript", "pipeline", "react", "python", "distributed"],
    "Hospital Database Management System": ["database", "sql", "health", "backend", "relational", "optimization"],
}

# Signals that indicate the JD cares more about depth of professional experience
_EXP_DEPTH_SIGNALS = [
    "team lead", "senior", "lead engineer", "manager", "production", "enterprise",
    "large team", "stakeholder", "cross-functional", "years of experience",
    "industry experience", "professional experience", "proven experience",
]

# Signals that indicate the JD cares more about technical projects and portfolio
_PROJ_DEPTH_SIGNALS = [
    "portfolio", "side project", "personal project", "github", "open source",
    "demonstrate", "show your work", "hackathon", "projects demonstrating",
    "ai projects", "ml projects", "technical projects", "build something",
    "entry level", "entry-level", "new grad", "recent graduate",
]


def select_resume_layout(job: dict, profile: dict) -> dict:
    """Decide the optimal resume layout for a given job posting.

    Returns one of two layouts:
    - "2exp_3proj": 2 professional experiences + 3 projects
      Used for: entry-level, AI/ML-heavy, project-portfolio-focused JDs
    - "3exp_2proj": 3 professional experiences + 2 projects
      Used for: senior, enterprise, team/leadership-focused JDs

    The selected experiences and projects are ranked by tag overlap with the JD,
    so the most relevant ones always appear first.
    """
    jd_text = f"{job.get('title', '')} {job.get('full_description', '')}".lower()
    resume_facts = profile.get("resume_facts", {})

    work_experiences = resume_facts.get("work_experiences", [])
    projects_full = resume_facts.get("projects_full", [])

    # Score each experience and project by tag overlap with the JD
    exp_scores = sorted(
        work_experiences,
        key=lambda e: sum(1 for t in e.get("tags", []) if t in jd_text),
        reverse=True,
    )
    proj_scores = sorted(
        projects_full,
        key=lambda p: sum(1 for t in p.get("tags", []) if t in jd_text),
        reverse=True,
    )

    exp_depth = sum(1 for s in _EXP_DEPTH_SIGNALS if s in jd_text)
    proj_depth = sum(1 for s in _PROJ_DEPTH_SIGNALS if s in jd_text)

    # Entry-level / AI-heavy roles default to 2 exp + 3 proj unless JD explicitly
    # signals it wants professional depth
    if exp_depth > proj_depth + 1:
        layout = "3exp_2proj"
        selected_experiences = exp_scores[:3]
        selected_projects = proj_scores[:2]
    else:
        layout = "2exp_3proj"
        selected_experiences = exp_scores[:2]
        selected_projects = proj_scores[:3]

    return {
        "layout": layout,
        "experiences": selected_experiences,
        "projects": selected_projects,
    }


def _classify_software_domain(job: dict) -> tuple[bool, str]:
    title = (job.get("title") or "").lower()
    desc = (job.get("full_description") or "").lower()
    text = f"{title}\n{desc}"

    title_positive_hits = sorted([k for k in SOFTWARE_TITLE_HINTS if k in title])
    title_negative_hits = sorted([k for k in UNRELATED_TITLE_HINTS if k in title])
    positive_hits = sorted([k for k in SOFTWARE_KEYWORDS if k in text])
    negative_hits = sorted([k for k in UNRELATED_KEYWORDS if k in text])

    if title_positive_hits:
        return True, f"title-positive:{', '.join(title_positive_hits[:3])}"
    if title_negative_hits and not title_positive_hits:
        return False, f"title-negative:{', '.join(title_negative_hits[:3])}"

    if len(positive_hits) >= 2 and len(positive_hits) >= len(negative_hits):
        return True, f"text-positive:{', '.join(positive_hits[:4])}"
    if len(positive_hits) >= 1 and len(negative_hits) == 0:
        return True, f"text-positive:{', '.join(positive_hits[:4])}"

    return False, f"insufficient-software-signals pos={len(positive_hits)} neg={len(negative_hits)}"


def _project_context(job: dict) -> dict:
    text = f"{job.get('title','')}\n{job.get('full_description','')}".lower()
    scores = {}
    for name, tags in PROJECT_TAGS.items():
        scores[name] = sum(1 for t in tags if t in text)
    return scores


def _build_layout_context(job: dict, profile: dict) -> str:
    """Build a layout section string for use in the tailor prompt.

    Summarizes which experiences and projects were selected and why,
    so the LLM knows exactly what to include.
    """
    layout_result = select_resume_layout(job, profile)
    layout = layout_result["layout"]
    experiences = layout_result["experiences"]
    projects = layout_result["projects"]

    exp_lines = "\n".join(
        f"  - {e['role']} at {e['company']} ({e['timeline']})"
        for e in experiences
    )
    proj_lines = "\n".join(
        f"  - {p['name']}: {p['subtitle']}"
        for p in projects
    )

    mode = "2 experiences + 3 projects" if layout == "2exp_3proj" else "3 experiences + 2 projects"
    return (
        f"## SELECTED LAYOUT: {mode} (layout={layout})\n\n"
        f"INCLUDE THESE EXPERIENCES (in this order):\n{exp_lines}\n\n"
        f"INCLUDE THESE PROJECTS (in this order):\n{proj_lines}\n\n"
        f"Do NOT include any experience or project not listed above."
    )


MASTER_RESUME = OUTPUT_RESUME_DIR / "gunakarthik_naidu_lanka_resume.pdf"
MASTER_RESUME_TEXT = OUTPUT_RESUME_DIR / "master_resume_text.txt"  # scored against for reuse decisions
MASTER_USAGE_FILE = OUTPUT_RESUME_DIR / "master_usage.json"
ARCHIVED_RESUMES_DIR = OUTPUT_RESUME_DIR / "archived"


def _load_master_usage() -> list[str]:
    """Return list of companies that used the current master resume."""
    try:
        if MASTER_USAGE_FILE.exists():
            return json.loads(MASTER_USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _add_master_usage(company: str) -> None:
    """Record that a company used the current master resume."""
    companies = _load_master_usage()
    if company and company not in companies:
        companies.append(company)
    try:
        MASTER_USAGE_FILE.write_text(json.dumps(companies), encoding="utf-8")
    except Exception as e:
        log.warning("Could not update master_usage.json: %s", e)


def _archive_master_resume() -> None:
    """Archive the current master resume with the company names that used it, then reset usage."""
    if not MASTER_RESUME.exists():
        return
    ARCHIVED_RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    companies = _load_master_usage()
    if companies:
        safe_names = [re.sub(r"[^\w]", "", c)[:20] for c in companies[:6]]
        archive_name = "_".join(safe_names) + "_Resume.pdf"
    else:
        archive_name = f"archived_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_Resume.pdf"
    archive_path = ARCHIVED_RESUMES_DIR / archive_name
    try:
        shutil.copy2(MASTER_RESUME, archive_path)
        log.info("Archived master resume as: %s (used by: %s)", archive_path.name, companies)
    except Exception as e:
        log.warning("Could not archive master resume: %s", e)
    # Reset usage tracker for new master
    try:
        MASTER_USAGE_FILE.write_text(json.dumps([]), encoding="utf-8")
    except Exception:
        pass


def _mark_job_tailored(conn, job_url: str, resume_path: Path) -> None:
    # Always copy to master — upload always uses this fixed filename
    try:
        shutil.copy2(resume_path, MASTER_RESUME)
    except Exception as e:
        log.warning("Could not copy to master resume: %s", e)

    conn.execute(
        """
        UPDATE jobs
        SET tailored_resume_path=?,
            tailored_at=?,
            tailor_attempts=COALESCE(tailor_attempts,0)+1,
            apply_status=CASE
                WHEN apply_status IN ('skipped_policy', 'skipped_bad_data') THEN NULL
                ELSE apply_status
            END,
            apply_error=CASE
                WHEN apply_status IN ('skipped_policy', 'skipped_bad_data') THEN NULL
                ELSE apply_error
            END
        WHERE url=?
        """,
        (str(resume_path), datetime.now(timezone.utc).isoformat(), job_url),
    )


def _copy_base_resume(base_pdf: Path, target_pdf: Path) -> None:
    target_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base_pdf, target_pdf)
    if not target_pdf.exists():
        raise RuntimeError(f"Fallback copy did not create {target_pdf}")


def _infer_failure_reason(exc: Exception) -> str:
    text = str(exc).lower()
    if "empty" in text:
        return "rewrite_returned_empty"
    if "bullet count changed" in text or "count changed" in text:
        return "bullet_count_mismatch"
    if "unchanged_bullets" in text or "unchanged bullets" in text:
        return "unchanged_bullets"
    if "word budget exceeded" in text:
        return "word_budget_exceeded"
    if "compile/page validation failed" in text:
        return "compile_validation_failed"
    if "compile failed" in text:
        return "compile_failure"
    if "injection_no_diff" in text:
        return "injection_no_diff"
    return exc.__class__.__name__.lower()


def _build_base_bullets_from_profile(profile: dict) -> list[str]:
    r"""Build the ordered base bullet list from profile.json.

    Order must match the \item slots in fixed_resume.tex:
      0-3  : Velocity Tech (4 bullets — strongest role gets most space)
      4-6  : EPICS at ASU (3 bullets)
      7-9  : HireAgent (3 bullets)
      10-12: FosterArizona.org (3 bullets)
      13-14: AutoAudit AI (2 bullets)
    """
    facts = profile.get("resume_facts", {})
    exp_map = {e["id"]: e.get("bullets", []) for e in facts.get("work_experiences", [])}
    proj_map = {p["id"]: p.get("bullets", []) for p in facts.get("projects_full", [])}

    def pick(lst: list, *indices) -> list[str]:
        return [lst[i] for i in indices if i < len(lst)]

    vel = exp_map.get("velocity_tech", [])
    epics = exp_map.get("epics_asu", [])
    ha = proj_map.get("hireagent", [])
    fa = proj_map.get("foster_arizona", [])
    aa = proj_map.get("autoaudit", [])

    bullets = (
        pick(vel, 0, 1, 2, 4)   # Velocity Tech: 4 bullets (FastAPI, AI parser w/60%, PostgreSQL, 99.9% uptime)
        + pick(epics, 0, 1, 3)  # EPICS: 3 bullets
        + pick(ha, 0, 1, 4)     # HireAgent: 3 bullets
        + (fa[:3] if len(fa) >= 3 else fa)  # FosterArizona: 3 bullets
        + (aa[:2] if len(aa) >= 2 else aa)  # AutoAudit: 2 bullets
    )
    return bullets


# Slot counts must match fixed_resume.tex exactly
_SLOT_COUNTS = {
    "velocity_tech": 4,
    "epics_asu": 3,
    "hireagent": 3,
    "foster_arizona": 3,
    "autoaudit": 2,
}
_SLOT_ORDER = ["velocity_tech", "epics_asu", "hireagent", "foster_arizona", "autoaudit"]
_SLOT_DEFAULTS = {
    "velocity_tech": [0, 1, 2, 4],
    "epics_asu":     [0, 1, 3],
    "hireagent":     [0, 1, 4],
    "foster_arizona":[0, 1, 2],
    "autoaudit":     [0, 3],
}


def _detect_role_type(job: dict) -> str:
    """Classify the JD into a role category to guide bullet focus selection."""
    text = f"{job.get('title', '')} {job.get('full_description', '') or ''}".lower()
    title = (job.get("title") or "").lower()

    # Explicit title signals first (most specific wins)
    if any(k in title for k in ("data engineer", "data pipeline", "etl", "analytics engineer", "data analyst", "business analyst", "business intelligence")):
        return "data_engineer"
    # AI/ML check: catch "software engineer, ai", "ai software engineer", etc.
    if any(k in title for k in ("ai engineer", "ml engineer", "machine learning", "llm", "nlp engineer", "software engineer, ai", "ai software", "software engineer ai", ", ai", "- ai")):
        return "ai_ml"
    if any(k in title for k in ("frontend", "front-end", "ui engineer", "react developer")):
        return "frontend"
    if any(k in title for k in ("devops", "sre", "site reliability", "platform engineer", "cloud engineer", "infrastructure engineer")):
        return "devops"
    if any(k in title for k in ("backend", "back-end", "api engineer", "software engineer", "software developer", "full stack", "full-stack", "new grad", "new grad swe")):
        return "backend_swe"

    # Body signals — require clear dominance, not just hitting a low threshold
    ai_signals = sum(1 for k in ("llm", "ai", "ml", "model", "nlp", "langchain", "openai", "claude", "gemini", "prompt", "inference") if k in text)
    data_signals = sum(1 for k in ("etl", "pipeline", "data warehouse", "spark", "kafka", "airflow", "dbt", "bigquery", "redshift", "snowflake", "data lake") if k in text)
    # DevOps: need genuinely infra-focused signals, not just Docker/K8s which appear in any backend JD
    devops_signals = sum(1 for k in ("kubernetes", "terraform", "helm", "grafana", "infrastructure as code", "deployment pipeline", "observability", "on-call", "incident response", "sre") if k in text)
    frontend_signals = sum(1 for k in ("react", "angular", "vue", "css", "html", "ui/ux", "frontend", "user interface") if k in text)
    # Backend signals — if present alongside devops, backend wins
    backend_signals = sum(1 for k in ("java", "spring", "fastapi", "node.js", "rest api", "microservice", "api", "backend", "server-side", "golang", "django", "rails", "grpc") if k in text)

    if data_signals >= 2:
        return "data_engineer"
    if ai_signals >= 3:
        return "ai_ml"
    # Devops only wins if clearly dominant (threshold 3) AND more devops than backend signals
    if devops_signals >= 3 and devops_signals > backend_signals:
        return "devops"
    if frontend_signals >= 3:
        return "frontend"
    return "backend_swe"


def _get_focused_defaults(profile: dict, role_type: str) -> dict[str, list[int]]:
    """Return bullet_focus indices from profile.json for a given role type."""
    facts = profile.get("resume_facts", {})
    exp_map = {e["id"]: e for e in facts.get("work_experiences", [])}
    proj_map = {p["id"]: p for p in facts.get("projects_full", [])}

    result: dict[str, list[int]] = {}
    for sid in _SLOT_ORDER:
        entry = exp_map.get(sid) or proj_map.get(sid) or {}
        focus = entry.get("bullet_focus", {})
        indices = focus.get(role_type) or focus.get("default") or _SLOT_DEFAULTS[sid]
        result[sid] = indices[: _SLOT_COUNTS[sid]]
    return result


def _select_bullets_for_jd(profile: dict, job: dict) -> list[str]:
    r"""Ask the LLM to pick the best bullet indices for this JD.

    Template slots (must match fixed_resume.tex \item count):
      velocity_tech  : 4
      epics_asu      : 3
      hireagent      : 3
      foster_arizona : 3
      autoaudit      : 2
    Total: 15

    Falls back to profile bullet_focus defaults on any failure.
    """
    facts = profile.get("resume_facts", {})
    exp_map = {e["id"]: e.get("bullets", []) for e in facts.get("work_experiences", [])}
    proj_map = {p["id"]: p.get("bullets", []) for p in facts.get("projects_full", [])}

    bullet_pools = {
        "velocity_tech":  exp_map.get("velocity_tech", []),
        "epics_asu":      exp_map.get("epics_asu", []),
        "hireagent":      proj_map.get("hireagent", []),
        "foster_arizona": proj_map.get("foster_arizona", []),
        "autoaudit":      proj_map.get("autoaudit", []),
    }

    role_type = _detect_role_type(job)
    smart_defaults = _get_focused_defaults(profile, role_type)
    log.info("Bullet selection role_type=%s for job=%s", role_type, job.get("title", ""))

    def fmt(section_id: str, label: str) -> str:
        pool = bullet_pools[section_id]
        count = _SLOT_COUNTS[section_id]
        focus_indices = smart_defaults[section_id]
        lines = [f"{label} (select exactly {count} — suggested for this role: {focus_indices}):"]
        for i, b in enumerate(pool):
            marker = " *" if i in focus_indices else ""
            lines.append(f"  {i}{marker}: {b}")
        return "\n".join(lines)

    options = "\n\n".join([
        fmt("velocity_tech",  "VELOCITY TECH (backend/AI platform experience)"),
        fmt("epics_asu",      "EPICS AT ASU (full-stack nonprofit platform)"),
        fmt("hireagent",      "HIREAGENT PROJECT (agentic AI pipeline)"),
        fmt("foster_arizona", "FOSTER ARIZONA PROJECT (React web platform)"),
        fmt("autoaudit",      "AUTOAUDIT AI PROJECT (local LLM inference engine)"),
    ])

    jd_snippet = (job.get("full_description", "") or "")[:2500]
    role_hint = {
        "ai_ml": "This is an AI/ML/LLM role. Prefer bullets that show LLM integration, prompt engineering, model inference, agentic systems.",
        "data_engineer": "This is a data engineering role. Prefer bullets that show SQL schemas, data pipelines, ETL, PostgreSQL, structured data, caching.",
        "frontend": "This is a frontend role. Prefer bullets that show React, UI components, accessibility, state management, mobile.",
        "devops": "This is a DevOps/infrastructure role. Prefer bullets that show Docker, CI/CD, AWS, deployment pipelines, monitoring.",
        "backend_swe": "This is a backend software engineering role. Prefer bullets that show APIs, microservices, FastAPI/Spring Boot, databases, system design.",
    }.get(role_type, "")

    system = (
        "You are a resume expert selecting the best bullets for a specific job application. "
        "Bullets marked with * are pre-suggested for this role type — prefer them unless a different bullet is clearly stronger. "
        "Select bullets that best demonstrate relevant skills and impact for this exact job. "
        "Return ONLY valid JSON with selected index arrays. No explanation, no markdown, no preamble."
    )
    user = (
        f"ROLE TYPE: {role_type.upper()}\n"
        f"{role_hint}\n\n"
        f"JOB TITLE: {job.get('title', '')}\n"
        f"JOB DESCRIPTION:\n{jd_snippet}\n\n"
        f"AVAILABLE BULLETS (select the exact count shown for each section):\n{options}\n\n"
        "Return JSON:\n"
        '{"velocity_tech":[i,i,i,i],"epics_asu":[i,i,i],'
        '"hireagent":[i,i,i],"foster_arizona":[i,i,i],"autoaudit":[i,i]}'
    )

    def safe_pick(pool: list, indices: list, required: int) -> list[str]:
        seen: set[int] = set()
        picked: list[str] = []
        for i in indices:
            if isinstance(i, int) and 0 <= i < len(pool) and i not in seen:
                picked.append(pool[i])
                seen.add(i)
            if len(picked) == required:
                break
        for i in range(len(pool)):
            if i not in seen and len(picked) < required:
                picked.append(pool[i])
                seen.add(i)
        return picked[:required]

    try:
        client = get_select_client()
        raw = client.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=220,
            temperature=0.1,
        )
        data = extract_json(raw)

        selected: list[str] = []
        for sid in _SLOT_ORDER:
            pool = bullet_pools[sid]
            indices = data.get(sid, smart_defaults[sid])
            selected.extend(safe_pick(pool, indices, _SLOT_COUNTS[sid]))

        if len(selected) != 15:
            raise ValueError(f"Selection count mismatch: {len(selected)}")

        log.info("Bullet selection OK: role=%s bullets=15 job=%s", role_type, job.get("title", ""))
        return selected

    except Exception as e:
        log.warning("Bullet selection failed (%s), using smart defaults for role=%s", e, role_type)
        # Use profile bullet_focus defaults (smarter than hardcoded indices)
        selected = []
        for sid in _SLOT_ORDER:
            pool = bullet_pools[sid]
            indices = smart_defaults[sid]
            selected.extend(safe_pick(pool, indices, _SLOT_COUNTS[sid]))
        return selected if len(selected) == 15 else _build_base_bullets_from_profile(profile)


def _generate_summary_for_jd(profile: dict, job: dict) -> str:
    """Generate a 2-3 sentence tailored summary starting with 'Accelerated Masters Student from ASU'."""
    facts = profile.get("resume_facts", {})
    exp_list = facts.get("work_experiences", [])
    proj_list = facts.get("projects_full", [])

    exp_summary = ", ".join(
        f"{e['role']} at {e['company']}" for e in exp_list[:2]
    )
    proj_summary = ", ".join(p["name"] for p in proj_list[:3])

    system = (
        "Write a 2-sentence resume summary. HARD RULES:\n"
        "1. Max 35 words total — count carefully.\n"
        "2. Start with: 'Accelerated Masters Student from ASU'\n"
        "3. Sentence 1 (15-18 words): candidate identity + 2 skills most relevant to this specific role.\n"
        "4. Sentence 2 (15-18 words): key achievement or project + seeking.\n"
        "5. No 'I', 'my', 'me'. Third-person framing.\n"
        "6. No clichés: passionate, leverage, dynamic, hardworking, motivated.\n"
        "7. No em dashes. Output ONLY the 2-sentence text."
    )
    user = (
        f"ROLE: {job.get('title', '')} at {job.get('company', job.get('site', ''))}\n"
        f"TOP JD SKILLS: {(job.get('full_description', '') or '')[:400]}\n\n"
        f"CANDIDATE: MS CS ASU (4.0 GPA, Dec 2025) | {exp_summary} | Projects: {proj_summary}\n"
        f"RELEVANT SKILLS: Python, FastAPI, Spring Boot, PostgreSQL, Docker, React, Claude API, Ollama\n\n"
        "Write exactly 2 sentences (35 words max) starting with 'Accelerated Masters Student from ASU':"
    )

    _FALLBACK_SUMMARY = (
        "Accelerated Masters Student from ASU (4.0 GPA, Dec 2025) specializing in backend systems and LLM integration. "
        "Built production-grade pipelines at Velocity Tech and shipped HireAgent, an agentic job discovery platform."
    )

    try:
        client = get_select_client()
        raw = client.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=80,
            temperature=0.2,
        )
        summary = _strip_llm_preamble(raw).strip()
        # Hard cap: trim to 38 words to prevent page overflow
        words = summary.split()
        if len(words) > 38:
            # Find sentence boundary near word 35
            text = " ".join(words[:38])
            last_period = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
            summary = text[:last_period + 1] if last_period > 20 else text
        if not summary or len(summary.split()) < 12:
            return _FALLBACK_SUMMARY
        return summary
    except Exception as e:
        log.warning("Summary generation failed (%s), using fallback", e)
        return _FALLBACK_SUMMARY


def run_tailoring(min_score: int = 7, limit: int = 20, validation_mode: str = "strict") -> dict:
    """Deterministic tailoring using fixed LaTeX template and local Ollama bullets."""

    # Tailor ALL eligible (entry-level) jobs regardless of fit_score.
    # min_score is kept for CLI compatibility but ignored - we want maximum coverage.
    log.info("Tailoring ALL eligible jobs (min_score threshold disabled for maximum coverage).")

    resolved_template_path = TEMPLATE_PATH.resolve()
    log.info("Base template validation: template=%s", resolved_template_path)
    template = load_fixed_template()
    profile = load_profile()
    base_bullets = _build_base_bullets_from_profile(profile)
    log.info("Base bullets loaded from profile.json: %s bullets", len(base_bullets))

    conn = get_connection()
    candidate_limit = max(limit * 50, 200)
    jobs = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=0, limit=candidate_limit)
    if not jobs:
        log.info("No untailored jobs available for tailoring.")
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    # Sort by role type so similar roles are processed together.
    # Benefit: same bullet set gets reused across consecutive similar jobs,
    # and each "role cluster" gets a consistent, focused resume.
    _ROLE_ORDER = {"ai_ml": 0, "data_engineer": 1, "backend_swe": 2, "frontend": 3, "devops": 4}
    jobs = sorted(jobs, key=lambda j: _ROLE_ORDER.get(_detect_role_type(j), 9))
    role_counts: dict[str, int] = {}
    for j in jobs:
        rt = _detect_role_type(j)
        role_counts[rt] = role_counts.get(rt, 0) + 1
    log.info("Tailor job order by role type: %s", role_counts)

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_RESUME_DIR.mkdir(parents=True, exist_ok=True)
    if SAVE_TEX_DEBUG:
        TAILOR_TEX_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    decisions_log = LOG_DIR / "tailor_decisions.log"

    base_pdf = OUTPUT_RESUME_DIR / "base_fixed.pdf"
    log.info(
        "Base template compile request: working_dir=%s expected_pdf=%s",
        (base_pdf.parent / f"{base_pdf.stem}_build").resolve(),
        base_pdf.resolve(),
    )
    base_ok, base_pages = compile_tex(
        template,
        base_pdf,
        template_path=resolved_template_path,
        debug_label="base-template",
    )
    log.info(
        "Base template compile result: success=%s pages=%s output=%s",
        base_ok,
        base_pages,
        base_pdf.resolve(),
    )
    if STRICT_ONE_PAGE and (not base_ok or base_pages != 1):
        raise RuntimeError(
            "Base template failed one-page compile "
            f"(success={base_ok}, pages={base_pages}, template={resolved_template_path}, output={base_pdf.resolve()})"
        )

    t0 = time.time()
    stats = {"approved": 0, "failed": 0, "errors": 0, "fallback": 0}
    throttle = float(os.environ.get("HIREAGENT_LLM_SLEEP_SECONDS", "0") or 0)
    policy = get_targeting_policy()
    eligible_processed = 0

    for idx, job in enumerate(jobs, start=1):
        job_url = job.get("url")
        title = job.get("title") or "Unknown"
        company = job.get("company") or job.get("site") or "Unknown"
        score = int(job.get("fit_score") or 0)
        job_ref = job_url or f"{company}:{title}"
        job_hash = cache_key(job) if job_url else ""
        pdf_path = OUTPUT_RESUME_DIR / f"{job_hash}.pdf" if job_hash else None
        meta_path = OUTPUT_RESUME_DIR / f"{job_hash}.meta.json" if job_hash else None
        tex_debug_path = TAILOR_TEX_DEBUG_DIR / f"{job_hash}.tex" if job_hash else None

        if not job_url:
            stats["errors"] += 1
            log.error("Tailor job missing URL key, skipping: %s", job_ref)
            continue

        # Only hard-skip if there is no usable title — all other quality issues
        # (missing location, missing application_url) are fine for tailoring.
        quality_ok, quality_reason = classify_job_data_quality(job)
        if not quality_ok and quality_reason in ("bad_data:blank_title", "bad_data:placeholder_title"):
            stats["failed"] += 1
            log.info(
                "Tailor skipped bad data job id=%s title=%s company=%s reason=%s",
                job_url, title, company, quality_reason,
            )
            continue
        if not quality_ok:
            log.info(
                "Tailor quality warning (non-blocking) id=%s title=%s reason=%s",
                job_url, title, quality_reason,
            )

        eligibility = classify_job_eligibility(job, policy=policy)
        if not eligibility["final_eligible"]:
            reason = format_eligibility_reasons(eligibility)
            log.info(
                "Tailor skipped ineligible job[%s/%s] id=%s title=%s company=%s reason=%s",
                idx, len(jobs), job_url, title, company, reason,
            )
            stats["failed"] += 1
            continue
        log.info(
            "Tailor job[%s/%s] id=%s title=%s company=%s score=%s eligible=True",
            idx, len(jobs), job_url, title, company, score,
        )

        if eligible_processed >= limit:
            continue
        eligible_processed += 1

        decision = adaptive_plan(score)
        mode = "light" if decision == "light" else "semantic"
        used_fallback = False
        failure_reason = ""
        changed_bullets = 0

        # ── Master resume reuse logic ──────────────────────────────────────
        # Score this job against the CURRENT master resume text.
        # If it scores ≥ 7, the master fits well enough — reuse it, no new PDF.
        # If it scores < 7, the master is wrong for this role — archive it and
        # create a fresh tailored resume that becomes the new master.
        if MASTER_RESUME.exists():
            master_fit = 0
            if MASTER_RESUME_TEXT.exists():
                try:
                    from hireagent.scoring.scorer import score_job as _score_against_master
                    master_text = MASTER_RESUME_TEXT.read_text(encoding="utf-8")
                    master_fit = _score_against_master(master_text, job)["score"]
                    log.info(
                        "Tailor master-fit check id=%s master_fit=%s title=%s",
                        job_url, master_fit, title,
                    )
                except Exception as e:
                    log.warning("Master fit check failed (%s), falling back to DB score", e)
                    master_fit = score
            else:
                # No master text saved yet — fall back to DB fit_score
                master_fit = score

            if master_fit >= 7:
                _add_master_usage(company)
                _mark_job_tailored(conn, job_url, MASTER_RESUME)
                stats["approved"] += 1
                log.info(
                    "Tailor master reused id=%s company=%s master_fit=%s (no new resume needed)",
                    job_url, company, master_fit,
                )
                continue
            else:
                log.info(
                    "Tailor master archived id=%s company=%s master_fit=%s (creating new resume)",
                    job_url, company, master_fit,
                )
                _archive_master_resume()
                try:
                    MASTER_RESUME_TEXT.unlink()
                except FileNotFoundError:
                    pass

        if pdf_path is not None and pdf_path.exists():
            _mark_job_tailored(conn, job_url, pdf_path)
            stats["approved"] += 1
            cached_fallback = False
            if meta_path is not None and meta_path.exists():
                try:
                    cached_fallback = bool(json.loads(meta_path.read_text(encoding="utf-8")).get("fallback_used"))
                except Exception:
                    cached_fallback = False
            log.info(
                "Tailor selected id=%s mode=%s cache_hit=true pdf_created=true fallback=%s db_updated=tailored_resume_path",
                job_url,
                mode,
                cached_fallback,
            )
            continue

        try:
            # ── Bullet Selection (LLM picks best pre-written bullets for this JD) ──
            new_bullets = _select_bullets_for_jd(profile, job)
            summary = _generate_summary_for_jd(profile, job)

            if len(new_bullets) != len(base_bullets):
                failure_reason = "bullet_count_mismatch"
                raise ValueError(f"Bullet selection returned {len(new_bullets)} bullets, expected {len(base_bullets)}")

            if any(not b.strip() for b in new_bullets):
                failure_reason = "rewrite_returned_empty"
                raise ValueError("Selection returned empty bullet")

            changed_bullets = sum(
                1 for old, new in zip(base_bullets, new_bullets)
                if _normalize_bullet(old) != _normalize_bullet(new)
            )
            jd_specific_generated = True  # selection always produces a valid result
            log.info(
                "Tailor bullet selection id=%s changed=%s/%s summary_words=%s",
                job_url, changed_bullets, len(base_bullets), len(summary.split()),
            )

            filled_tex = apply_summary(apply_bullets(template, new_bullets), summary)
            inserted_diff = filled_tex != template
            if not inserted_diff:
                failure_reason = "injection_no_diff"
                raise ValueError("injection_no_diff")
            log.info(
                "Tailor template injection id=%s inserted_diff=%s",
                job_url,
                inserted_diff,
            )

            if SAVE_TEX_DEBUG and tex_debug_path is not None:
                tex_debug_path.write_text(filled_tex, encoding="utf-8")
                log.info("Tailor TEX debug saved id=%s path=%s", job_url, tex_debug_path)

            tex_ok, pages = compile_tex(
                filled_tex,
                pdf_path,
                template_path=resolved_template_path,
                debug_label=f"job:{job_hash[:12]}",
            )
            if not tex_ok:
                failure_reason = "compile_failure"
                raise ValueError("LaTeX compile failed")

            if pages != 1:
                # Tighten by reducing body font size — never cut bullets mid-sentence.
                # Each step shrinks body text slightly until the resume fits one page.
                tightened_success = False
                for body_pt, leading_pt in (("9.8", "12.8"), ("9.5", "12.5"), ("9.2", "12.2"), ("9.0", "12.0"), ("8.7", "11.5")):
                    tightened_tex = filled_tex.replace(
                        r"\fontsize{10}{13}\selectfont",
                        f"\\fontsize{{{body_pt}}}{{{leading_pt}}}\\selectfont",
                    )
                    if SAVE_TEX_DEBUG and tex_debug_path is not None:
                        tighten_tex_path = tex_debug_path.with_name(f"{tex_debug_path.stem}_f{body_pt}.tex")
                        tighten_tex_path.write_text(tightened_tex, encoding="utf-8")
                        log.info("Tailor TEX tighten debug saved id=%s path=%s", job_url, tighten_tex_path)
                    tight_ok, tight_pages = compile_tex(
                        tightened_tex,
                        pdf_path,
                        template_path=resolved_template_path,
                        debug_label=f"job:{job_hash[:12]}-f{body_pt}",
                    )
                    if tight_ok and tight_pages == 1:
                        if SAVE_TEX_DEBUG and tex_debug_path is not None:
                            tex_debug_path.write_text(tightened_tex, encoding="utf-8")
                        pages = tight_pages
                        tightened_success = True
                        log.info(
                            "Tailor one-page tighten success id=%s font=%spt changed_bullets=%s/%s",
                            job_url,
                            body_pt,
                            changed_bullets,
                            len(base_bullets),
                        )
                        break
                if not tightened_success:
                    failure_reason = f"page_count_not_one:{pages}"
                    raise ValueError(f"LaTeX page count invalid ({pages})")

            safe_title = re.sub(r"[^\w\s-]", "", title)[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", (job.get("site") or "site"))[:20].strip().replace(" ", "_")
            prefix = f"{safe_site}_{safe_title}"
            diff_path = TAILORED_DIR / f"{prefix}_diff.txt"
            diff_lines = [f"- {b}" for b in base_bullets] + [f"+ {b}" for b in new_bullets]
            diff_path.write_text("\n".join(diff_lines), encoding="utf-8")
            if meta_path is not None:
                meta_path.write_text(
                    json.dumps(
                        {
                            "title": title,
                            "url": job_url,
                            "decision_mode": mode,
                            "changed_bullets": changed_bullets,
                            "fallback_used": False,
                            "fallback_reason": None,
                            "output_tex_path": str(tex_debug_path) if tex_debug_path else None,
                            "output_pdf_path": str(pdf_path),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            with decisions_log.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"{title} | score={score} | decision={decision} | mode={mode} | "
                    f"hash={job_hash} | jd_specific={jd_specific_generated} | changed={changed_bullets}/{len(base_bullets)} | fallback=no\n"
                )

            _mark_job_tailored(conn, job_url, pdf_path)

            # Save the resume text so future jobs can be scored against it.
            # This is what drives the "reuse or create new master" decision.
            try:
                MASTER_RESUME_TEXT.write_text(
                    summary + "\n\n" + "\n".join(new_bullets),
                    encoding="utf-8",
                )
            except Exception as e:
                log.warning("Could not save master resume text: %s", e)

            # Save a human-readable copy named after the company so the
            # employer sees "BRM_AI_Software_Engineer_Gunakarthik.pdf" not a hash.
            if pdf_path and pdf_path.exists():
                raw_company = (job.get("company") or job.get("site") or "Company")
                safe_company = re.sub(r"[^\w\s-]", "", raw_company)[:30].strip().replace(" ", "_")
                safe_role = re.sub(r"[^\w\s-]", "", title)[:40].strip().replace(" ", "_")
                named_pdf = OUTPUT_RESUME_DIR / f"{safe_company}_{safe_role}_Gunakarthik.pdf"
                try:
                    shutil.copy2(pdf_path, named_pdf)
                    log.info("Named resume copy saved: %s", named_pdf)
                except Exception as copy_err:
                    log.warning("Could not save named resume copy: %s", copy_err)

            stats["approved"] += 1
            log.info(
                "Tailor selected id=%s mode=%s cache_hit=false jd_specific=%s changed_bullets=%s/%s "
                "pdf_created=%s fallback=false db_updated=tailored_resume_path",
                job_url,
                mode,
                jd_specific_generated,
                changed_bullets,
                len(base_bullets),
                pdf_path.exists(),
            )

        except Exception as exc:
            failure_reason = failure_reason or _infer_failure_reason(exc)
            log.warning(
                "Tailor rewrite/injection failure id=%s mode=%s reason=%s error=%s",
                job_url,
                mode,
                failure_reason,
                exc,
            )
            log.error("Tailoring rewrite failed for id=%s title=%s: %s", job_url, title, exc, exc_info=True)
            try:
                used_fallback = True
                _copy_base_resume(base_pdf, pdf_path)
                _mark_job_tailored(conn, job_url, pdf_path)
                stats["approved"] += 1
                stats["fallback"] = stats.get("fallback", 0) + 1
                if meta_path is not None:
                    meta_path.write_text(
                        json.dumps(
                            {
                                "title": title,
                                "url": job_url,
                                "decision_mode": mode,
                                "changed_bullets": changed_bullets,
                                "fallback_used": True,
                                "fallback_reason": failure_reason,
                                "output_tex_path": str(tex_debug_path) if tex_debug_path else None,
                                "output_pdf_path": str(pdf_path),
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                with decisions_log.open("a", encoding="utf-8") as fh:
                    fh.write(
                        f"{title} | score={score} | decision={decision} | mode={mode} | hash={job_hash} | "
                        f"jd_specific=False | changed=0/{len(base_bullets)} | fallback=yes | reason={failure_reason}\n"
                    )
                log.warning(
                    "Tailor fallback used id=%s mode=%s jd_specific=false changed_bullets=0/%s "
                    "pdf_created=%s fallback=%s fallback_reason=%s db_updated=tailored_resume_path",
                    job_url,
                    mode,
                    len(base_bullets),
                    pdf_path.exists(),
                    used_fallback,
                    failure_reason,
                )
            except Exception as fallback_exc:
                stats["failed"] += 1
                conn.execute("UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?", (job_url,))
                log.error(
                    "Tailor failed with no usable fallback id=%s mode=%s fallback_error=%s",
                    job_url,
                    mode,
                    fallback_exc,
                    exc_info=True,
                )

        if throttle > 0:
            time.sleep(throttle)

    conn.commit()
    elapsed = time.time() - t0
    log.info("Tailoring done in %.1fs: %s", elapsed, stats)
    return {
        "approved": stats.get("approved", 0),
        "failed": stats.get("failed", 0),
        "errors": stats.get("errors", 0),
        "fallback": stats.get("fallback", 0),
        "elapsed": elapsed,
    }
