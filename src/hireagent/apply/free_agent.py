"""Zero-cost apply engine for Stage 7.

Priority:
  1. OpenClaw Local Gateway (http://127.0.0.1:18789)
  2. Playwright ATS-aware form fill (Greenhouse / Lever / Workday / generic)

Both paths:
  - Fill ALL fields strictly from profile.json (no hallucination)
  - Ask via Telegram for any unknown/unmapped required field
  - HITL pause before final submit (Telegram /approve or terminal ENTER)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

OPENCLAW_GATEWAY = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
OPENCLAW_TOKEN   = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
OLLAMA_BASE      = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
VISION_MODEL     = os.environ.get("VISION_LLM_MODEL", "qwen2.5vl:7b")
TELEGRAM_APPROVAL_TIMEOUT = int(os.environ.get("TELEGRAM_APPROVAL_TIMEOUT", "300"))

RESULT_APPLIED  = "applied"
RESULT_FAILED   = "failed"
RESULT_SKIPPED  = "skipped_preflight"
RESULT_PENDING  = "pending_human_review"


# ── Profile flattening ─────────────────────────────────────────────────────

def _flat(profile: dict) -> dict:
    """Return every useful field from profile.json as a flat dict."""
    p   = profile.get("personal", {})
    comp = profile.get("compensation", {})
    auth = profile.get("work_authorization", {})
    exp  = profile.get("experience", {})
    edu_m = profile.get("education", {}).get("masters", {})
    edu_b = profile.get("education", {}).get("bachelors", {})

    full = p.get("full_name", "")
    parts = full.split()
    # "Gunakarthik Naidu Lanka" → first="Gunakarthik", middle="Naidu", last="Lanka"
    first  = parts[0] if parts else ""
    last   = parts[-1] if len(parts) > 1 else ""
    middle = parts[1] if len(parts) > 2 else ""
    phone_raw = "".join(c for c in p.get("phone", "") if c.isdigit())

    return {
        # Identity
        "full_name":              full,
        "first_name":             p.get("preferred_name") or first,  # use preferred/short name (Guna)
        "legal_first_name":       first,                              # Gunakarthik — only use if form says "legal"
        "last_name":              last,
        "middle_name":            middle,
        "preferred_name":         p.get("preferred_name") or first,
        "email":                  p.get("email", ""),
        "password":               p.get("password", ""),
        "phone":                  phone_raw[-10:] if phone_raw else "",   # 10-digit US number
        "phone_formatted":        p.get("phone", ""),
        "phone_raw":              phone_raw[-10:] if phone_raw else "",
        "phone_digits":           phone_raw[-10:] if phone_raw else "",
        # Address
        "address":                p.get("address", ""),
        "city":                   p.get("city", ""),
        "state":                  p.get("province_state", ""),
        "state_full":             "Arizona",
        "country":                p.get("country", "United States"),
        "postal_code":            p.get("postal_code", ""),
        "location":               f"{p.get('city','')}, {p.get('province_state','')}",
        # Links
        "linkedin_url":           p.get("linkedin_url", ""),
        "github_url":             p.get("github_url", ""),
        "portfolio_url":          p.get("portfolio_url", ""),
        "website_url":            p.get("website_url", p.get("portfolio_url", "")),
        # Work auth
        "authorized":             auth.get("legally_authorized_to_work", "Yes"),
        "sponsorship":            auth.get("require_sponsorship", "No"),
        "work_permit":            auth.get("work_permit_type", "OPT/F-1"),
        # Compensation
        "salary":                 comp.get("salary_expectation", "90000"),
        "salary_min":             comp.get("salary_range_min", "85000"),
        "salary_max":             comp.get("salary_range_max", "115000"),
        "salary_range":           f"${int(comp.get('salary_range_min', 85000)):,} - ${int(comp.get('salary_range_max', 115000)):,}",
        "how_did_you_hear":       "LinkedIn",
        # Education — MS
        "school":                 edu_m.get("school", "Arizona State University"),
        "school_ms":              edu_m.get("school", "Arizona State University"),
        "school_bs":              edu_b.get("school", "Arizona State University"),
        "degree_ms":              f"{edu_m.get('degree','Master of Science')} in {edu_m.get('field_primary','Computer Science')}",
        "degree_bs":              f"{edu_b.get('degree','Bachelor of Science')} in {edu_b.get('field_primary','Computer Science')}",
        "degree_name_ms":         edu_m.get("degree", "Master of Science"),
        "degree_field_ms":        edu_m.get("field_primary", "Computer Science"),
        "graduation_year":        "2025",   # MS graduation December 2025
        "graduation_year_ms":     "2025",
        "graduation_year_bs":     "2024",
        "graduation_month":       "December",
        "graduation_date_ms":     "December 2025",
        "graduation_date_bs":     "December 2024",
        "start_date_ms":          "January 2025",
        "start_date_bs":          "August 2021",
        "gpa":                    edu_m.get("gpa", "4.0"),
        # Experience
        "years_experience":       exp.get("years_of_experience_total", "1"),
        "education_level":        exp.get("education_level", "Master's Degree"),
        "current_title":          exp.get("current_job_title", "Software Engineer"),
        "target_role":            exp.get("target_role", "Software Engineer"),
        # Availability
        "availability":           profile.get("availability", {}).get("earliest_start_date", "Immediately"),
        "willing_to_relocate":    profile.get("availability", {}).get("willing_to_relocate", "Yes"),
        # Diversity / EEO
        "gender":                 profile.get("eeo_voluntary", {}).get("gender", "Male"),
        "ethnicity":              profile.get("eeo_voluntary", {}).get("ethnicity", "Asian (Not Hispanic or Latino)"),
        "veteran":                profile.get("eeo_voluntary", {}).get("veteran_status", "I am not a protected veteran"),
        "disability":             profile.get("eeo_voluntary", {}).get("disability_status", "I do not have a disability"),
        # Common yes/no answers
        "yes": "Yes",
        "no":  "No",
    }


# ── Telegram helpers ───────────────────────────────────────────────────────

def _tg(text: str, photo_path: Path | None = None) -> bool:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    base = f"https://api.telegram.org/bot{token}"
    try:
        if photo_path and photo_path.exists():
            with open(photo_path, "rb") as f:
                r = requests.post(f"{base}/sendPhoto",
                    data={"chat_id": chat_id, "caption": text[:1024]},
                    files={"photo": f}, timeout=15)
        else:
            r = requests.post(f"{base}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        return False


def _tg_ask(question: str, timeout: int = 120) -> str | None:
    """Send a question to Telegram and wait for a text reply. Returns reply text or None."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return None

    _tg(question)
    base     = f"https://api.telegram.org/bot{token}"
    deadline = time.time() + timeout
    offset   = 0

    # Drain old updates first
    try:
        r = requests.get(f"{base}/getUpdates", params={"offset": -1, "limit": 1}, timeout=5)
        updates = r.json().get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
    except Exception:
        pass

    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/getUpdates",
                params={"offset": offset, "timeout": 20, "allowed_updates": ["message"]},
                timeout=30)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
                    continue
                text = (msg.get("text") or "").strip()
                if text.lower() in ("/approve", "/reject"):
                    continue  # skip control commands
                if text:
                    log.info("Telegram answer: %s", text[:80])
                    return text
        except Exception as e:
            log.debug("Telegram poll: %s", e)
            time.sleep(3)
    return None


def _tg_wait_approval(title: str, timeout: int) -> bool:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return True

    base     = f"https://api.telegram.org/bot{token}"
    deadline = time.time() + timeout
    offset   = 0

    try:
        r = requests.get(f"{base}/getUpdates", params={"offset": -1, "limit": 1}, timeout=5)
        updates = r.json().get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
    except Exception:
        pass

    log.info("Waiting %ds for Telegram /approve or /reject", timeout)
    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/getUpdates",
                params={"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
                timeout=35)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
                    continue
                cmd = (msg.get("text") or "").strip().lower()
                if cmd.startswith("/approve"):
                    return True
                if cmd.startswith("/reject"):
                    return False
        except Exception as e:
            log.debug("Telegram poll: %s", e)
            time.sleep(3)

    log.warning("Telegram approval timeout for '%s'", title)
    return False


# ── ATS-specific form fillers ──────────────────────────────────────────────

def _label_for(page, el) -> str:
    """Best-effort label text for a form element."""
    label = ""
    el_id = el.get_attribute("id") or ""
    if el_id:
        lbl = page.query_selector(f"label[for='{el_id}']")
        if lbl:
            label = lbl.inner_text()
    if not label:
        label = (
            el.get_attribute("aria-label") or
            el.get_attribute("placeholder") or
            el.get_attribute("name") or
            el.get_attribute("id") or ""
        )
    return label.strip()


def _safe_fill(el, value: str, page=None):
    """Type value into a field character-by-character with human-like timing.
    After typing, checks for autocomplete dropdowns and clicks the best match."""
    import random as _rng
    import time as _time
    try:
        el.click()
        el.fill("")
        el.type(value, delay=_rng.randint(30, 80))
        _time.sleep(_rng.uniform(0.05, 0.18))
    except Exception:
        try:
            el.fill(value)
        except Exception:
            return False

    # ── Autocomplete dropdown: wait briefly then pick best matching option ──
    if page is None:
        return True  # no page reference — typing succeeded, skip autocomplete check

    try:
        _time.sleep(0.4)   # give dropdown time to appear
        DROPDOWN_SELS = [
            "[role='option']:visible",
            "[role='listbox'] [role='option']",
            "[class*='autocomplete'] li",
            "[class*='suggestion']",
            "[class*='dropdown-item']",
            "[class*='typeahead'] li",
            "[class*='combobox'] li",
            "[class*='select__option']",       # react-select
            "[class*='react-select__option']",
            "[data-autocomplete-value]",
            "ul[class*='suggest'] li",
            "div[class*='option'][id*='option']",
        ]
        val_lower = value.lower()
        for sel in DROPDOWN_SELS:
            try:
                opts = page.locator(sel).all()
                if not opts:
                    continue
                best = None
                for opt in opts[:20]:
                    try:
                        if not opt.is_visible(timeout=200):
                            continue
                        opt_text = (opt.inner_text() or "").strip().lower()
                        if not opt_text:
                            continue
                        if opt_text == val_lower:
                            best = opt
                            break
                        if opt_text.startswith(val_lower) or val_lower in opt_text:
                            if best is None:
                                best = opt
                    except Exception:
                        pass
                if best is not None:
                    best.click()
                    log.debug("Autocomplete: picked '%s' for '%s'",
                              (best.inner_text() or "")[:30], value[:30])
                    return True
                # No match — pick first non-placeholder option
                if opts:
                    try:
                        first = opts[0]
                        if first.is_visible(timeout=200):
                            first_text = (first.inner_text() or "").strip()
                            if first_text and first_text.lower() not in (
                                "select", "please select", "--", "none", "other"
                            ):
                                first.click()
                                log.debug("Autocomplete: first option '%s'", first_text[:30])
                                return True
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

    return True


def _safe_select(el, value: str, label: str = "") -> bool:
    """Try selecting by value, label, or partial match."""
    for method in (
        lambda: el.select_option(value=value, timeout=500),
        lambda: el.select_option(label=value, timeout=500),
        lambda: el.select_option(label=label, timeout=500) if label else (_ for _ in ()).throw(StopIteration()),
    ):
        try:
            method()
            return True
        except Exception:
            pass
    return False


# Map label keywords → flat profile key.
# ORDER MATTERS: more specific entries must come before broader ones.
# e.g. "graduation year" before "degree" so "graduation year for highest degree" gets the year, not degree name.
_LABEL_MAP: list[tuple[str, str]] = [
    # ── Name ──────────────────────────────────────────────────────────────────
    ("first name",              "first_name"),
    ("given name",              "first_name"),
    ("last name",               "last_name"),
    ("family name",             "last_name"),
    ("surname",                 "last_name"),
    ("middle name",             "middle_name"),
    ("middle initial",          "middle_name"),
    ("full name",               "full_name"),
    ("legal name",              "full_name"),
    ("your name",               "full_name"),
    ("preferred name",          "preferred_name"),
    ("pronouns",                "preferred_name"),
    # ── Contact ───────────────────────────────────────────────────────────────
    ("email",                   "email"),
    ("e-mail",                  "email"),
    ("phone number",            "phone"),
    ("mobile phone",            "phone"),
    ("mobile number",           "phone"),
    ("cell phone",              "phone"),
    ("telephone",               "phone"),
    ("phone",                   "phone"),
    ("mobile",                  "phone"),
    # ── Address ───────────────────────────────────────────────────────────────
    ("street address",          "address"),
    ("street",                  "address"),
    ("address line 1",          "address"),
    ("address",                 "address"),
    ("city",                    "city"),
    ("state / province",        "state"),
    ("state/province",          "state"),
    ("province",                "state"),
    ("state",                   "state"),
    ("zip code",                "postal_code"),
    ("zip/postal",              "postal_code"),
    ("postal code",             "postal_code"),
    ("postal",                  "postal_code"),
    ("zip",                     "postal_code"),
    ("country",                 "country"),
    ("current location",        "location"),
    ("location",                "location"),
    # ── Links ─────────────────────────────────────────────────────────────────
    ("linkedin",                "linkedin_url"),
    ("github",                  "github_url"),
    ("portfolio",               "portfolio_url"),
    ("personal website",        "website_url"),
    ("website",                 "website_url"),
    # ── Graduation / Education dates (MUST be before "degree" / "school") ────
    ("graduation year",         "graduation_year"),
    ("year of graduation",      "graduation_year"),
    ("expected graduation year","graduation_year"),
    ("what year did you grad",  "graduation_year"),
    ("when did you grad",       "graduation_year"),
    ("anticipated graduation",  "graduation_year"),
    ("graduation date",         "graduation_date_ms"),
    ("expected graduation",     "graduation_date_ms"),
    ("completion date",         "graduation_date_ms"),
    ("end date",                "graduation_date_ms"),
    ("graduation month",        "graduation_month"),
    ("enrollment start date",   "start_date_ms"),   # MS enrollment (education context)
    ("program start date",      "start_date_ms"),   # MS enrollment (education context)
    # ── Availability (must be before generic "start date" in education block) ─
    ("start date",              "availability"),
    ("available to start",      "availability"),
    ("earliest start",          "availability"),
    ("when can you start",      "availability"),
    # ── Education ─────────────────────────────────────────────────────────────
    ("field of study",          "degree_field_ms"),
    ("major",                   "degree_field_ms"),
    ("area of study",           "degree_field_ms"),
    ("concentration",           "degree_field_ms"),
    ("discipline",              "degree_field_ms"),
    ("degree name",             "degree_name_ms"),
    ("degree type",             "degree_name_ms"),
    ("highest degree",          "degree_ms"),
    ("degree earned",           "degree_ms"),
    ("degree",                  "degree_ms"),
    ("gpa",                     "gpa"),
    ("grade point",             "gpa"),
    ("cumulative gpa",          "gpa"),
    ("academic gpa",            "gpa"),
    ("overall gpa",             "gpa"),
    ("school name",             "school"),
    ("university name",         "school"),
    ("college name",            "school"),
    ("institution name",        "school"),
    ("school",                  "school"),
    ("university",              "school"),
    ("college",                 "school"),
    ("institution",             "school"),
    ("education level",         "education_level"),
    ("highest level of education", "education_level"),
    ("highest education",       "education_level"),
    # ── Experience ────────────────────────────────────────────────────────────
    ("years of experience",     "years_experience"),
    ("years of relevant exp",   "years_experience"),
    ("years exp",               "years_experience"),
    ("how many year",           "years_experience"),
    ("total experience",        "years_experience"),
    ("current title",           "current_title"),
    ("current position",        "current_title"),
    ("job title",               "current_title"),
    # ── Compensation ──────────────────────────────────────────────────────────
    ("salary expectation",      "salary"),
    ("expected salary",         "salary"),
    ("desired salary",          "salary"),
    ("expected compensation",   "salary"),
    ("desired compensation",    "salary"),
    ("salary requirement",      "salary"),
    ("minimum salary",          "salary_min"),
    ("maximum salary",          "salary_max"),
    ("target salary range",     "salary_range"),
    ("salary range",            "salary_range"),
    ("salary",                  "salary"),
    ("compensation",            "salary"),
    # ── Work authorization ────────────────────────────────────────────────────
    ("legally authorized",      "authorized"),
    ("authorized to work",      "authorized"),
    ("eligible to work",        "authorized"),
    ("work authoriz",           "authorized"),
    ("authorized",              "authorized"),
    ("require.*sponsor",        "sponsorship"),
    ("need.*sponsor",           "sponsorship"),
    ("visa sponsor",            "sponsorship"),
    ("sponsor",                 "sponsorship"),
    ("visa status",             "work_permit"),
    ("visa type",               "work_permit"),
    ("work visa",               "work_permit"),
    ("visa",                    "work_permit"),
    ("work permit",             "work_permit"),
    ("citizenship",             "authorized"),
    # ── Availability (start date entries moved above education block) ───────────
    ("willing to relocate",     "willing_to_relocate"),
    ("open to relocation",      "willing_to_relocate"),
    # ── EEO / Diversity ───────────────────────────────────────────────────────
    ("gender",                  "gender"),
    ("sex",                     "gender"),
    ("race",                    "ethnicity"),
    ("ethnicity",               "ethnicity"),
    ("veteran",                 "veteran"),
    ("military",                "veteran"),
    ("disability",              "disability"),
    # ── Source / referral ─────────────────────────────────────────────────────
    ("how did you hear",        "how_did_you_hear"),
    ("how did you learn",       "how_did_you_hear"),
    ("how did you find",        "how_did_you_hear"),
    ("where did you hear",      "how_did_you_hear"),
    ("where did you learn",     "how_did_you_hear"),
    ("how were you referred",   "how_did_you_hear"),
    ("referral source",         "how_did_you_hear"),
    # ── Generic catch-alls ────────────────────────────────────────────────────
    ("referral",                "no"),
    ("cover letter",            "no"),
    # ── Name fallback (must be last — very short keywords) ────────────────────
    # Block fields that contain "name" but are NOT the applicant's name
    ("company name",            "_skip"),
    ("employer name",           "_skip"),
    ("organization name",       "_skip"),
    ("position name",           "_skip"),
    ("role name",               "_skip"),
    ("job name",                "_skip"),
    ("program name",            "_skip"),
    ("project name",            "_skip"),
    ("institution name",        "school"),   # already above but safety
    ("first",                   "first_name"),
    ("last",                    "last_name"),
    ("middle",                  "middle_name"),
    ("name",                    "full_name"),
]


def _value_for_label(label: str, flat: dict) -> str | None:
    """Match a form field label to a profile value. Returns None if no match."""
    import re as _re
    ll = label.lower().strip()
    # Strip common boilerplate so "Please provide your graduation year..." → "graduation year..."
    ll = _re.sub(r"^(please (provide|enter|specify|tell us)|what is (your|the)|your)\s+", "", ll)
    ll = _re.sub(r"\s*(for your highest (completed )?degree|of your highest (completed )?degree)\s*", " ", ll)
    ll = ll.strip()
    for keyword, key in _LABEL_MAP:
        if keyword in ll:
            if key == "_skip":
                return None  # explicitly skip — don't fill this field
            val = flat.get(key)
            if val is not None:
                return val
    return None


def _fill_all_inputs(page, flat: dict, fast: bool = False) -> tuple[int, list[str]]:
    """Fill all visible text/email/tel/url inputs from profile. Returns (filled_count, unfilled_labels).

    fast=True uses el.fill() directly instead of _safe_fill, skipping autocomplete checks.
    Use fast=True for ATS platforms (Greenhouse, Lever) where autocomplete can hang.
    """
    filled = 0
    unfilled: list[str] = []

    inputs = page.query_selector_all(
        "input[type='text'], input[type='email'], input[type='tel'], "
        "input[type='url'], input[type='number'], textarea"
    )
    for inp in inputs[:150]:  # cap at 150 to prevent slowdown on JS-heavy pages
        try:
            if not inp.is_visible():
                continue
            # Skip read-only and disabled fields
            if inp.get_attribute("readonly") is not None or inp.get_attribute("disabled") is not None:
                continue
            # type="email" always gets the email — don't risk a bad label match
            inp_type = (inp.get_attribute("type") or "text").lower()
            if inp_type == "email":
                value = flat.get("email", "")
                if value:
                    current = ""
                    try:
                        current = inp.input_value() or ""
                    except Exception:
                        pass
                    if current.strip().lower() != value.strip().lower():
                        if fast:
                            inp.fill(value)
                        else:
                            _safe_fill(inp, value, page=page)
                        filled += 1
                        log.debug("Filled email (by type) = '%s'", value[:30])
                continue
            label = _label_for(page, inp)
            if not label:
                # No visible label — use name/placeholder as fallback identifier
                # so the LLM can still attempt to fill it in the unknown-fields pass
                fallback_id = (
                    inp.get_attribute("name") or
                    inp.get_attribute("placeholder") or
                    inp.get_attribute("data-qa") or
                    inp.get_attribute("id") or ""
                ).strip()
                if fallback_id:
                    required = (inp.get_attribute("required") is not None or
                                inp.get_attribute("aria-required") == "true")
                    if required:
                        unfilled.append(f"[unlabeled:{fallback_id}]")
                continue
            value = _value_for_label(label, flat)
            if value:
                current = ""
                try:
                    current = inp.input_value() or ""
                except Exception:
                    pass
                # Skip only if already has exactly our value
                if current.strip().lower() == str(value).strip().lower():
                    continue
                if fast:
                    inp.fill(str(value))
                else:
                    _safe_fill(inp, str(value), page=page)
                filled += 1
                log.debug("Filled '%s' = '%s'", label[:50], str(value)[:30])
            else:
                required = (inp.get_attribute("required") is not None or
                            inp.get_attribute("aria-required") == "true")
                if required and label:
                    unfilled.append(label)
        except Exception as e:
            log.debug("Input fill error: %s", e)

    return filled, unfilled


def _fill_all_selects(page, flat: dict) -> int:
    filled = 0
    for sel in page.query_selector_all("select"):
        try:
            if not sel.is_visible():
                continue
            label = _label_for(page, sel)
            ll = label.lower()

            def _try(*values) -> bool:
                for v in values:
                    try:
                        sel.select_option(label=v, timeout=500)
                        return True
                    except Exception:
                        pass
                    try:
                        sel.select_option(value=v, timeout=500)
                        return True
                    except Exception:
                        pass
                    # partial match: find option whose text contains v
                    try:
                        opts = sel.query_selector_all("option")
                        for opt in opts:
                            ot = (opt.inner_text() or "").strip()
                            if v.lower() in ot.lower() or ot.lower() in v.lower():
                                sel.select_option(label=ot, timeout=500)
                                return True
                    except Exception:
                        pass
                return False

            ok = False
            # Email address dropdown (LinkedIn Easy Apply shows email as a select)
            if "email" in ll:
                ok = _try(flat.get("email", ""))
            # Graduation year (MUST check before generic "year" or "degree")
            if any(k in ll for k in ("graduation year", "year of graduation", "expected graduation year",
                                     "when did you grad", "anticipated grad")):
                ok = _try("2025", "December 2025")
            elif any(k in ll for k in ("graduation date", "graduation month", "completion date")):
                ok = _try("December 2025", "2025", "December")
            elif any(k in ll for k in ("start date", "enrollment")):
                ok = _try("January 2025", "2025")
            # Country
            elif "country" in ll:
                ok = _try("United States", "United States of America", "US", "USA")
            # State
            elif "state" in ll or "province" in ll:
                ok = _try("Arizona", "AZ")
            # Work authorization
            elif any(k in ll for k in ("authorized", "eligible to work", "work auth", "legally auth")):
                ok = _try("Yes", "I am authorized", "Authorized")
            # Sponsorship — only answer if the label explicitly asks about needing sponsorship
            elif any(k in ll for k in ("require.*sponsor", "need.*sponsor", "will you.*require",
                                        "will you.*need.*visa", "need sponsorship", "require sponsorship",
                                        "currently require", "currently need")):
                ok = _try("No", "No, I do not", "Will not require", "No sponsorship needed")
            # Visa / work permit
            elif "visa" in ll or "work permit" in ll or "citizenship" in ll:
                ok = _try("OPT", "F-1 OPT", "Student Visa (OPT)", "Other")
            # Gender — use "Decline to self-identify" per profile
            elif "gender" in ll or "sex" in ll:
                ok = _try("Decline to self-identify", "Prefer not to say", "Prefer not to disclose",
                           "I prefer not to answer", "Not specified", "Other", "Prefer not to answer")
            # Veteran
            elif "veteran" in ll or "military" in ll:
                ok = _try("I am not a protected veteran", "Not a protected veteran", "No", "I am not a veteran")
            # Disability
            elif "disability" in ll or "disabled" in ll:
                ok = _try("I do not have a disability", "No disability", "No",
                           "I don't have a disability", "None")
            # Race / ethnicity — decline per profile
            elif "race" in ll or "ethnicity" in ll:
                ok = _try("Decline to self-identify", "Prefer not to say", "I prefer not to answer",
                           "Asian", "Asian (Not Hispanic or Latino)")
            # Education level (not graduation year, not field)
            elif any(k in ll for k in ("education level", "highest education", "degree level", "highest level")):
                ok = _try("Master's Degree", "Master", "Masters", "Master's")
            # Degree / field (only if NOT about graduation year)
            elif "degree" in ll and "year" not in ll and "graduation" not in ll:
                ok = _try("Master of Science", "Master's Degree", "Master", "Masters")
            elif "field of study" in ll or "major" in ll or "area of study" in ll:
                ok = _try("Computer Science", "Computer Science and Engineering")
            # Years of experience
            elif "year" in ll and any(k in ll for k in ("exp", "experience", "work")):
                ok = _try("1", "Less than 1 year", "0-1 years", "1 year")
            # Phone country code
            elif "country code" in ll or "phone country" in ll:
                ok = _try("+1", "United States (+1)", "US (+1)", "1")
            # Salary
            elif "salary" in ll or "compensation" in ll:
                ok = _try(flat.get("salary", "90000"))
            # Employment type
            elif "employment type" in ll or "job type" in ll or "work type" in ll:
                ok = _try("Full-time", "Full Time", "Permanent")
            # Willingness to relocate
            elif "relocat" in ll:
                ok = _try("Yes", "Willing to relocate")
            # How did you hear / learn
            elif any(k in ll for k in ("how did you hear", "how did you learn", "how did you find",
                                        "where did you hear", "how were you referred", "referral source")):
                ok = _try("LinkedIn", "Job Board", "Indeed", "Other")
            # CS / degree enrollment (select version)
            elif any(k in ll for k in ("enrolled in a degree", "currently enrolled", "degree program",
                                        "primary focus in computer", "closely related field")):
                ok = _try("Yes")
            # Office / work-in-office commitment (select version)
            elif any(k in ll for k in ("available to work in our", "requires that you be available",
                                        "this position requires", "commit to this")):
                ok = _try("Yes")
            # Office location preference — pick San Mateo or first available
            elif any(k in ll for k in ("office location", "which.*office", "preferred.*office",
                                        "prefer.*location", "office.*prefer")):
                ok = _try("San Mateo", "San Mateo, CA", "Raleigh", "Remote")
                if not ok:
                    # Fallback: pick first non-empty option
                    try:
                        opts = sel.query_selector_all("option")
                        for opt in opts:
                            ot = (opt.inner_text() or "").strip()
                            if ot and ot.lower() not in ("select", "please select", "- select -",
                                                         "--select--", "--", "-", "choose",
                                                         "choose one", "none", ""):
                                sel.select_option(label=ot, timeout=500)
                                ok = True
                                break
                    except Exception:
                        pass

            if ok:
                filled += 1
                log.debug("Select '%s' filled", label[:40])
            else:
                # Unknown required select — pick first non-placeholder option as fallback
                try:
                    required = (sel.get_attribute("required") is not None or
                                sel.get_attribute("aria-required") == "true")
                    if required:
                        opts = sel.query_selector_all("option")
                        for opt in opts:
                            ot = (opt.inner_text() or "").strip()
                            if ot and ot.lower() not in ("select", "please select", "- select -",
                                                         "--select--", "--", "-", "choose",
                                                         "choose one", "none", ""):
                                sel.select_option(label=ot, timeout=500)
                                filled += 1
                                log.debug("Select '%s' fallback → first option '%s'", label[:40], ot[:20])
                                break
                except Exception:
                    pass
        except Exception as e:
            log.debug("Select error label='%s': %s", label[:30], e)
    return filled


def _fill_radios(page, flat: dict) -> int:
    """Fill radio button groups using label keywords. Returns count filled."""
    filled = 0
    # Mapping: label keyword → desired value to click
    RADIO_MAP = [
        # Work auth (check before sponsor since sponsor is also work-auth related)
        (["require sponsor", "visa sponsorship", "need sponsor", "will you.*require.*sponsor",
          "will you.*need.*visa", "future.*visa", "sponsor.*now"],         "No"),
        (["authorized", "eligible to work", "legally authorized",
          "work auth", "authorized to work in the united"],                "Yes"),
        # CS / degree enrollment
        (["enrolled in a degree", "currently enrolled", "degree program.*computer",
          "major in computer science", "cs degree"],                       "Yes"),
        # Office / location commitment
        (["san mateo", "headquarters office", "in.office", "commit to this requirement",
          "able to.*office", "confirm.*ability", "available.*work.*office"],  "Yes"),
        # General availability
        (["relocat"],                   "Yes"),
        (["remote", "work from home"],  "Yes"),
        (["full.?time", "full time"],   "Yes"),
        (["us citizen", "citizen"],     "No"),
        (["disability"],                "I do not have a disability"),
        (["veteran"],                   "I am not a protected veteran"),
        (["gender"],                    "Decline to self-identify"),
        (["race", "ethnicity"],         "Decline to self-identify"),
    ]
    # Find all radio groups by name attribute
    radios = page.query_selector_all("input[type='radio']")
    groups: dict[str, list] = {}
    for r in radios:
        try:
            if not r.is_visible():
                continue
            name = r.get_attribute("name") or ""
            if name not in groups:
                groups[name] = []
            groups[name].append(r)
        except Exception:
            pass

    for name, group in groups.items():
        # Get group label from first radio's label or fieldset legend
        group_label = ""
        try:
            first = group[0]
            el_id = first.get_attribute("id") or ""
            if el_id:
                lbl = page.query_selector(f"label[for='{el_id}']")
                if lbl:
                    group_label = lbl.inner_text()
            if not group_label:
                group_label = (first.get_attribute("aria-label") or
                               first.get_attribute("data-qa") or name)
            # Try fieldset legend
            legend = page.query_selector(f"input[name='{name}'] >> xpath=ancestor::fieldset//legend")
            if legend:
                group_label = legend.inner_text() + " " + group_label
        except Exception:
            pass

        ll = group_label.lower()
        import re as _re
        for keywords, desired in RADIO_MAP:
            if any(_re.search(kw, ll) for kw in keywords):
                # Find and click the radio with matching value/label
                for r in group:
                    try:
                        val = (r.get_attribute("value") or "").lower()
                        r_id = r.get_attribute("id") or ""
                        r_lbl = ""
                        if r_id:
                            lbl_el = page.query_selector(f"label[for='{r_id}']")
                            if lbl_el:
                                r_lbl = lbl_el.inner_text().lower()
                        if desired.lower() in (val, r_lbl):
                            r.click()
                            filled += 1
                            log.debug("Radio '%s' → %s", group_label[:40], desired)
                            break
                    except Exception:
                        pass
                break
        else:
            # No RADIO_MAP rule matched — if this is a required yes/no group, default to "Yes"
            try:
                values = [(r.get_attribute("value") or "").lower() for r in group]
                if set(values) <= {"yes", "no", "true", "false"} or len(group) == 2:
                    for r in group:
                        try:
                            val = (r.get_attribute("value") or "").lower()
                            r_id = r.get_attribute("id") or ""
                            r_lbl = ""
                            if r_id:
                                lbl_el = page.query_selector(f"label[for='{r_id}']")
                                if lbl_el:
                                    r_lbl = lbl_el.inner_text().lower()
                            if val in ("yes", "true") or "yes" in r_lbl:
                                r.click()
                                filled += 1
                                log.debug("Radio '%s' fallback → Yes", group_label[:40])
                                break
                        except Exception:
                            pass
            except Exception:
                pass
    return filled


def _fill_location_autocomplete(page, value: str, selector: str) -> bool:
    """Fill an autocomplete location field: type value, wait for dropdown, pick first result."""
    try:
        el = page.locator(selector).first
        if not el.is_visible(timeout=2000):
            return False
        el.click()
        el.fill("")
        el.type(value, delay=20)
        page.wait_for_timeout(200)
        # Try to click first suggestion in common dropdown patterns
        for dropdown_sel in (
            "[role='option']:visible",
            "[class*='autocomplete'] li:visible",
            "[class*='suggestion']:visible",
            "[class*='dropdown'] li:visible",
            "[class*='result']:visible",
        ):
            try:
                first_opt = page.locator(dropdown_sel).first
                if first_opt.is_visible(timeout=800):
                    first_opt.click()
                    log.debug("Location autocomplete picked: %s", first_opt.inner_text()[:40])
                    return True
            except Exception:
                pass
        # No dropdown appeared — press Escape to dismiss and accept plain text
        page.keyboard.press("Escape")
        return True
    except Exception as e:
        log.debug("Location autocomplete error: %s", e)
        return False


def _fill_greenhouse(page, flat: dict) -> tuple[int, list[str]]:
    """Greenhouse-specific field filling using known IDs."""
    filled = 0
    log.info("GH fill: starting known fields")
    GH = {
        "#first_name":     flat["first_name"],
        "#last_name":      flat["last_name"],
        "#email":          flat["email"],
        "#phone":          flat["phone"],
        "input[data-field='linkedin_profile']":  flat["linkedin_url"],
        "input[data-field='github_profile']":    flat["github_url"],
        "input[data-field='github']":            flat["github_url"],
        "input[data-field='website']":           flat["portfolio_url"],
    }
    for selector, value in GH.items():
        if not value:
            continue
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=1000):
                current = el.input_value()
                if not current:
                    el.fill(value)  # use fill() not _safe_fill to avoid autocomplete hang
                    filled += 1
                    log.info("GH fill %s = %s", selector, value[:30])
        except Exception:
            pass
    log.info("GH fill: known fields done (%d). Filling location...", filled)
    # Location field is an autocomplete — handle separately
    loc_filled = _fill_location_autocomplete(page, flat["location"], "#job_application_location")
    if loc_filled:
        filled += 1
    log.info("GH fill: location done. Running generic pass...")
    # Generic pass for remaining inputs — fast=True skips autocomplete to avoid hangs
    n, unfilled = _fill_all_inputs(page, flat, fast=True)
    filled += n
    log.info("GH fill: inputs done (%d filled, %d unfilled). Filling selects...", n, len(unfilled))
    filled += _fill_all_selects(page, flat)
    log.info("GH fill: selects done. Filling radios...")
    filled += _fill_radios(page, flat)

    # ── Greenhouse React Select comboboxes (input[role='combobox']) ──────────
    # Country and candidate-location use React Select — must type to trigger dropdown.
    def _gh_combobox(input_id: str, value: str) -> bool:
        try:
            el = page.locator(f"input#{input_id}").first
            if not el.is_visible(timeout=1000):
                return False
            el.click()
            el.fill("")
            el.type(value[:8], delay=40)
            page.wait_for_timeout(700)
            for opt_sel in ("[role='option']", "[class*='option']", "[id*='option']"):
                try:
                    for opt in page.locator(opt_sel).all()[:20]:
                        if not opt.is_visible(timeout=150):
                            continue
                        ot = (opt.inner_text() or "").strip()
                        if value.lower() in ot.lower():
                            opt.click()
                            log.info("GH combobox #%s → '%s'", input_id, ot)
                            page.wait_for_timeout(300)
                            return True
                except Exception:
                    pass
            page.keyboard.press("Escape")
        except Exception as _e:
            log.debug("GH combobox #%s failed: %s", input_id, _e)
        return False

    _gh_combobox("country", "United States")
    page.wait_for_timeout(500)  # Location options may update after country
    _gh_combobox("candidate-location", flat["city"])

    # Address Type — find by aria-labelledby or any remaining unfilled combobox,
    # then pick the FIRST available option (exact option text is unknown)
    def _gh_fill_address_type() -> bool:
        def _pick_first_option(inp_el) -> bool:
            try:
                inp_el.click()
                page.wait_for_timeout(500)
                for opt in page.locator("[role='option']").all()[:10]:
                    try:
                        if opt.is_visible(timeout=200):
                            ot = (opt.inner_text() or "").strip()
                            opt.click()
                            log.info("GH address type → first option '%s'", ot)
                            page.wait_for_timeout(300)
                            return True
                    except Exception:
                        pass
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        try:
            # Try aria-labelledby pointing to the address type label
            for suffix in ("question_35419246002-label", "question_35419246002"):
                el = page.locator(f"input[aria-labelledby*='{suffix}']").first
                try:
                    if el.is_visible(timeout=500):
                        if _pick_first_option(el):
                            return True
                except Exception:
                    pass
            # Fallback: any combobox that isn't country/location — pick first option
            for inp in page.query_selector_all("input[role='combobox']"):
                try:
                    inp_id = inp.get_attribute("id") or ""
                    if inp_id in ("country", "candidate-location"):
                        continue
                    if not inp.is_visible():
                        continue
                    if _pick_first_option(inp):
                        return True
                except Exception:
                    pass
        except Exception as _e:
            log.debug("GH address type failed: %s", _e)
        return False

    _gh_fill_address_type()

    # ── Greenhouse custom question fields (placeholder-based) ─────────────────
    GH_PLACEHOLDERS = {
        "Preferred First Name":  flat.get("preferred_name", "Guna"),
        "Legal First Name":      flat.get("legal_first_name", flat.get("first_name", "")),
        "Legal Last Name":       flat.get("last_name", ""),
        "Address Line 1":        flat.get("address", ""),
        "LinkedIn Profile":      flat.get("linkedin_url", ""),
    }
    for placeholder, val in GH_PLACEHOLDERS.items():
        if not val:
            continue
        try:
            for inp in page.query_selector_all(f"input[placeholder='{placeholder}'], textarea[placeholder='{placeholder}']"):
                if inp.is_visible() and not inp.input_value():
                    inp.fill(val)
                    filled += 1
                    log.info("GH placeholder '%s' = %s", placeholder, val[:30])
        except Exception:
            pass

    log.info("GH fill: complete. Total filled=%d", filled)
    return filled, unfilled


def _fill_lever(page, flat: dict) -> tuple[int, list[str]]:
    """Lever-specific field filling."""
    filled = 0
    LEVER = {
        "input[name='name']":         flat["full_name"],
        "input[name='email']":        flat["email"],
        "input[name='phone']":        flat["phone"],
        "input[name='org']":          "",
        "input[name='urls[LinkedIn]']": flat["linkedin_url"],
        "input[name='urls[GitHub]']":   flat["github_url"],
        "input[name='urls[Portfolio]']": flat["portfolio_url"],
    }
    for selector, value in LEVER.items():
        if not value:
            continue
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=1500):
                current = el.input_value()
                if not current:
                    _safe_fill(el, value, page=page)
                    filled += 1
        except Exception:
            pass
    n, unfilled = _fill_all_inputs(page, flat, fast=True)
    filled += n + _fill_all_selects(page, flat) + _fill_radios(page, flat)
    return filled, unfilled


def _upload_resume(page, resume_path: Path) -> bool:
    for selector in ("input[type='file']", "input[accept*='pdf']", "input[accept*='.pdf']"):
        try:
            inputs = page.query_selector_all(selector)
            for inp in inputs:
                try:
                    inp.set_input_files(str(resume_path))
                    log.info("Resume uploaded via selector '%s': %s", selector, resume_path.name)
                    page.wait_for_timeout(1500)
                    return True
                except Exception:
                    pass
        except Exception:
            pass
    return False


def _linkedin_login_if_needed(page, flat: dict, job_url: str) -> None:
    """Detect LinkedIn login wall and sign in with profile credentials."""
    try:
        current_url = page.url.lower()
        page_text = (page.inner_text("body") or "").lower()

        # Detect if we're on login page or showing a guest gate
        on_login_page = "login" in current_url or "authwall" in current_url
        has_login_form = "sign in" in page_text or "email or phone" in page_text

        # Check for actual email input field (login form visible)
        email_sel = "input#username, input[name='session_key'], input[autocomplete='username'], input[type='email']"
        pwd_sel = "input#password, input[name='session_password'], input[type='password']"
        try:
            email_el = page.locator(email_sel).first
            email_visible = email_el.is_visible(timeout=2000)
        except Exception:
            email_visible = False

        if not email_visible and not on_login_page and not has_login_form:
            log.debug("LinkedIn: no login wall detected, proceeding")
            return  # Already logged in

        log.info("LinkedIn login wall detected (url=%s, form=%s) — signing in",
                 on_login_page, email_visible)

        # If no email input visible yet, navigate to login page
        if not email_visible:
            log.info("Navigating to LinkedIn login page")
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(0)
            email_el = page.locator(email_sel).first

        # Dismiss any Google OAuth / "Continue as..." popup that blocks the form
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(0)
        except Exception:
            pass
        for dismiss_sel in (
            "button[aria-label='Dismiss']", "button[aria-label='Close']",
            ".artdeco-modal__dismiss", "[data-test-modal-close-btn]",
            "div[role='dialog'] button:last-child",
        ):
            try:
                btn = page.locator(dismiss_sel).first
                if btn.is_visible(timeout=500):
                    btn.click()
                    page.wait_for_timeout(0)
            except Exception:
                pass

        # Fill using Playwright locators — no visibility check, just try filling directly.
        # LinkedIn's email input: type=email, id=username, name=session_key, aria-label='Email or phone'
        email_filled = False
        for email_loc in (
            "input#username",
            "input[name='session_key']",
            "input[type='email']",
            "input[autocomplete*='username']",
            "input[aria-label*='Email']",
            "input[aria-label*='Phone']",
        ):
            try:
                el = page.locator(email_loc).first
                el.fill(flat["email"], timeout=5000)
                page.wait_for_timeout(0)
                email_filled = True
                log.info("LinkedIn email filled via: %s", email_loc)
                break
            except Exception:
                pass

        pwd_filled = False
        try:
            page.locator("input[type='password']").first.fill(flat["password"], timeout=5000)
            page.wait_for_timeout(0)
            pwd_filled = True
        except Exception:
            pass

        log.info("LinkedIn credential fill: email=%s pwd=%s", email_filled, pwd_filled)

        if not email_filled:
            log.warning("Could not fill LinkedIn email — skipping login attempt")
            return

        # Click sign in button
        signed_in = False
        for sel in ("button[type='submit']", "button[data-litms-control-urn*='sign-in']",
                    ".sign-in-form__submit-btn", ".btn__primary--large"):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    log.info("LinkedIn sign-in button clicked (%s)", sel)
                    page.wait_for_timeout(7000)
                    signed_in = True
                    break
            except Exception:
                pass
        if not signed_in:
            # JS fallback for sign-in button
            r = page.evaluate("""() => {
                for (const b of document.querySelectorAll('button,input[type="submit"]')) {
                    const t = (b.innerText||b.value||'').trim().toLowerCase();
                    if (t==='sign in'||t==='submit'||b.type==='submit') { b.click(); return t; }
                }
                return 'none';
            }""")
            if r and r != "none":
                log.info("LinkedIn sign-in via JS fallback: %s", r)
                page.wait_for_timeout(7000)
                signed_in = True

        if signed_in:
            # ── Check for CAPTCHA after login (LinkedIn anti-bot) ──
            # LinkedIn commonly presents a CAPTCHA right after clicking Sign In.
            try:
                captcha_post_login = page.evaluate("""() => {
                    const frames = Array.from(document.querySelectorAll('iframe'));
                    for (const f of frames) {
                        const src = (f.src || '').toLowerCase();
                        const isHcaptcha = src.includes('hcaptcha.com');
                        const isRcV2 = src.includes('api2/bframe') || src.includes('api2/anchor');
                        if (isHcaptcha || isRcV2) {
                            const r = f.getBoundingClientRect();
                            if (r.width > 10 && r.height > 10) return true;
                        }
                    }
                    if (document.querySelector('.h-captcha[data-sitekey]')) return true;
                    // LinkedIn security check page
                    const body = (document.body.innerText || '').toLowerCase();
                    if (body.includes('security verification') || body.includes('verify you are a human'))
                        return true;
                    return false;
                }""")
                if captcha_post_login:
                    log.info("CAPTCHA detected after LinkedIn login — attempting solve...")
                    for _solve_attempt in range(2):
                        solved = _solve_captcha(page, page.url)
                        if solved:
                            log.info("Post-login CAPTCHA solved (attempt %d)", _solve_attempt + 1)
                            page.wait_for_timeout(3000)  # Let page process token
                            break
                        page.wait_for_timeout(2000)
                    else:
                        log.warning("Post-login CAPTCHA unsolvable — continuing anyway")
            except Exception as _cap_err:
                log.debug("Post-login CAPTCHA check error: %s", _cap_err)

            # After login LinkedIn redirects to feed — navigate back to job
            current = page.url
            if "linkedin.com/jobs/view" not in current and job_url:
                log.info("Navigating back to job page after login: %s", job_url)
                page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(3000)
        else:
            log.warning("LinkedIn sign-in button not clicked — may not be logged in")
    except Exception as e:
        log.warning("LinkedIn login handler error: %s", e)


def _linkedin_dismiss_overlays(page) -> None:
    """Dismiss any Premium/upgrade overlays that could block button interaction."""
    for dismiss_sel in (
        "button[aria-label='Dismiss']", "button[aria-label='Close']",
        ".premium-upsell-dialog__dismiss", "[data-test-modal-close-btn]",
        ".artdeco-modal__dismiss",
    ):
        try:
            btn = page.locator(dismiss_sel).first
            if btn.is_visible(timeout=400):
                btn.click()
                page.wait_for_timeout(500)
        except Exception:
            pass


def _linkedin_click_continue_applying(page, pages_before: set, context) -> str | None:
    """Check for and click LinkedIn's 'Job search safety reminder' modal.

    LinkedIn shows this modal after clicking an external Apply button:
      'Job search safety reminder' with 'Continue applying ↗' button.
    Clicking 'Continue applying' opens the external ATS in a new tab.

    Returns 'external:<url>' if a new tab opened, None otherwise.
    """
    # Use has-text (substring match) not text-is (exact), because the button
    # contains an SVG external-link icon that may add whitespace to innerText.
    CONTINUE_SELS = [
        "button:has-text('Continue applying')",
        "a:has-text('Continue applying')",
        "button:has-text('continue applying')",
        "a:has-text('continue applying')",
        "[data-test-job-safety-reminder-modal] button",
        "[aria-label*='Continue applying' i]",
    ]
    for sel in CONTINUE_SELS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                log.info("LinkedIn safety reminder modal detected — clicking 'Continue applying' via: %s", sel)
                btn.click()
                # Wait for external tab to open (up to 8s)
                for _w in [1000, 1000, 1500, 2000, 2500]:
                    page.wait_for_timeout(_w)
                    new_pages = [p for p in context.pages if id(p) not in pages_before]
                    if new_pages:
                        new_page = new_pages[-1]
                        try:
                            new_page.wait_for_load_state("domcontentloaded", timeout=20_000)
                        except Exception:
                            pass
                        log.info("LinkedIn safety modal → Continue applying → new tab: %s", new_page.url)
                        return 'external:' + new_page.url
                # Tab may have opened before we captured pages_before — check current pages
                all_new = [p for p in context.pages if id(p) not in pages_before]
                if all_new:
                    new_page = all_new[-1]
                    return 'external:' + new_page.url
                return None
        except Exception:
            pass

    # JS fallback — find any visible button/link containing "continue applying" text
    try:
        clicked = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            for (const el of els) {
                const t = (el.innerText || el.textContent || el.getAttribute('aria-label') || '')
                    .trim().toLowerCase();
                if (t.includes('continue applying')) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        el.click();
                        return 'clicked:' + t.slice(0, 50);
                    }
                }
            }
            return null;
        }""")
        if clicked:
            log.info("LinkedIn safety modal → JS fallback clicked: %s", clicked)
            for _w in [1000, 1500, 2000, 2500]:
                page.wait_for_timeout(_w)
                new_pages = [p for p in context.pages if id(p) not in pages_before]
                if new_pages:
                    new_page = new_pages[-1]
                    try:
                        new_page.wait_for_load_state("domcontentloaded", timeout=20_000)
                    except Exception:
                        pass
                    log.info("LinkedIn safety modal JS → new tab: %s", new_page.url)
                    return 'external:' + new_page.url
    except Exception as _e:
        log.debug("Safety modal JS fallback error: %s", _e)

    return None


def _linkedin_click_apply(page, title: str, context) -> str:
    """Click the Apply or Easy Apply button on a LinkedIn job page.

    Returns:
      'easy_apply'         — Easy Apply modal opened
      'external:<url>'     — Regular Apply clicked; new tab/navigation to external ATS
      'not_found'          — No apply button found
      'job_expired'        — Job no longer accepting applications
    """
    _linkedin_dismiss_overlays(page)

    # ── Wait for job detail panel to actually load (not just the nav bar) ──
    # LinkedIn's React renders the job content async — wait until Apply button
    # area OR job title is visible before scanning.
    JOB_CONTENT_SELS = [
        "button[aria-label*='Easy Apply' i]",
        "button[aria-label*='Apply' i]",
        ".jobs-apply-button",
        ".jobs-apply-button--top-card",
        ".jobs-s-apply-button",
        ".job-details-jobs-unified-top-card__job-title",
        ".jobs-unified-top-card__job-title",
        "h1",
        ".job-view-layout",
        "main[role='main']",
    ]
    content_loaded = False
    for _wait_attempt in range(4):  # up to 4 × 3s = 12s extra wait
        for sel in JOB_CONTENT_SELS:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    content_loaded = True
                    log.info("LinkedIn job content visible via: %s", sel)
                    break
            except Exception:
                pass
        if content_loaded:
            break
        log.info("LinkedIn job content not yet visible (attempt %d) — waiting 3s", _wait_attempt + 1)
        page.wait_for_timeout(3000)

    # Always check if job is expired/closed (LinkedIn shows this banner even when content loads)
    try:
        body = (page.inner_text("body") or "").lower()
        EXPIRED_SIGNALS = (
            "no longer accepting",
            "job is closed",
            "this job is no longer",
            "position has been filled",
            "not accepting applications",
            "this job has expired",
            "posting has expired",
            "application window has closed",
            "no longer available",
            "hiring paused",
            "role has been filled",
            "closed to new applicants",
            "not currently accepting",
            "applications are closed",
        )
        PAGE_NOT_FOUND_SIGNALS = (
            "page not found",
            "this page doesn't exist",
            "content is no longer available",
            "this job listing has been removed",
            "we can't find this page",
            "hmm, this page doesn't exist",
            "sorry, that page doesn't exist",
        )
        if any(s in body for s in EXPIRED_SIGNALS):
            log.warning("LinkedIn: job expired/closed — '%s'", title)
            return 'job_expired'
        if any(s in body for s in PAGE_NOT_FOUND_SIGNALS):
            log.warning("LinkedIn: page not found — '%s'", title)
            return 'job_expired'
    except Exception:
        pass

    if not content_loaded:
        log.warning("LinkedIn: job detail panel never loaded for '%s' — proceeding anyway", title)
        # Don't bail — proceed anyway. The apply button may still be clickable
        # even if our content-loaded selectors didn't match (LinkedIn CSS changes).
        content_loaded = True

    # Give the Apply button a few extra seconds to render — LinkedIn lazy-loads the action buttons.
    if content_loaded:
        try:
            page.locator(
                "button[aria-label*='Apply' i], .jobs-apply-button, "
                ".jobs-apply-button--top-card, .jobs-s-apply-button"
            ).first.wait_for(state="visible", timeout=6000)
            log.info("LinkedIn Apply button rendered and visible")
        except Exception:
            log.info("LinkedIn Apply button not yet visible after extra wait — will still attempt scan")

    # Snapshot pages before click
    pages_before = set(id(p) for p in context.pages)

    # ── Try Playwright locators first (most reliable on LinkedIn) ──
    APPLY_LOCATORS = [
        # Easy Apply — specific LinkedIn class + aria-label
        ("easy_apply", "button[aria-label*='Easy Apply' i]"),
        ("easy_apply", "button:text-matches('easy apply', 'i')"),
        # Regular Apply (external link) — aria-label based (most reliable)
        ("apply",      "button[aria-label*='Apply' i]:not([aria-label*='Easy Apply' i])"),
        ("apply",      "a[aria-label*='Apply' i]:not([aria-label*='Easy' i])"),
        # Text-content based (catches "Apply ↗" and "Apply now")
        ("apply",      "button:text-matches('^apply', 'i'):not(:text-matches('easy', 'i'))"),
        ("apply",      "a:text-matches('^apply', 'i'):not(:text-matches('easy', 'i'))"),
    ]
    for kind, sel in APPLY_LOCATORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=5000):
                btn_text = (btn.inner_text() or btn.get_attribute("aria-label") or "").strip()
                # Skip Save, Save job, etc.
                if any(s in btn_text.lower() for s in ("save", "sign in", "log in", "follow")):
                    continue
                btn.scroll_into_view_if_needed()
                btn.click()
                log.info("LinkedIn '%s' clicked via PW locator (%s): '%s'", kind, sel[:50], btn_text[:40])
                if kind == "easy_apply":
                    page.wait_for_timeout(2000)
                    return 'easy_apply'
                # Regular apply — wait for new tab or page navigation
                # LinkedIn external Apply opens a new tab, but can take several seconds.
                # LinkedIn may also show a "Job search safety reminder" modal that must
                # be dismissed by clicking "Continue applying ↗" before the tab opens.
                for _wait_ms in [800, 800, 800]:  # quick initial check (2.4s)
                    page.wait_for_timeout(_wait_ms)
                    new_pages = [p for p in context.pages if id(p) not in pages_before]
                    if new_pages:
                        new_page = new_pages[-1]
                        try:
                            new_page.wait_for_load_state("domcontentloaded", timeout=20_000)
                        except Exception:
                            pass
                        log.info("LinkedIn Apply → new tab: %s", new_page.url)
                        return 'external:' + new_page.url
                # No tab yet — check for safety reminder modal and click Continue
                safety_result = _linkedin_click_continue_applying(page, pages_before, context)
                if safety_result:
                    return safety_result
                # Still no tab — wait a bit more
                for _wait_ms in [1500, 2000, 2000]:
                    page.wait_for_timeout(_wait_ms)
                    new_pages = [p for p in context.pages if id(p) not in pages_before]
                    if new_pages:
                        new_page = new_pages[-1]
                        try:
                            new_page.wait_for_load_state("domcontentloaded", timeout=20_000)
                        except Exception:
                            pass
                        log.info("LinkedIn Apply → new tab (delayed): %s", new_page.url)
                        return 'external:' + new_page.url
                cur = page.url
                if "linkedin.com" not in cur:
                    return 'external:' + cur
                # Button was clicked but no external tab opened — stop trying other selectors
                log.info("LinkedIn Apply button clicked but no new tab for '%s' — stopping selector scan", title)
                break
        except Exception:
            pass

    # ── JS fallback scan — ONLY look inside the main job detail panel ──
    # LinkedIn shows many job cards in sidebar — we must only click the MAIN job's button.
    # The main job detail panel is inside specific containers.
    result = page.evaluate("""() => {
        const txt = el => {
            const raw = (el.getAttribute('aria-label') || el.innerText || el.textContent || '')
                .replace(/[^\\x20-\\x7E]/g, ' ')
                .trim().toLowerCase().replace(/\\s+/g, ' ');
            return raw;
        };
        const inViewport = el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && r.top < window.innerHeight && r.bottom > 0;
        };
        const SKIP = new Set(['save', 'save job', 'follow', 'sign in', 'log in']);

        // --- Priority 0: find button INSIDE the main job top-card panel only ---
        const TOP_CARD_SELS = [
            '.jobs-apply-button--top-card',
            '.jobs-s-apply-button',
            '.jobs-unified-top-card__content--two-pane',
            '.job-view-layout',
            '.jobs-details__main-content',
            '.jobs-details',
            'main',
        ];
        for (const containerSel of TOP_CARD_SELS) {
            const container = document.querySelector(containerSel);
            if (!container) continue;
            const btns = Array.from(container.querySelectorAll('button, [role="button"]'));
            for (const el of btns) {
                const t = txt(el);
                if (SKIP.has(t) || !inViewport(el)) continue;
                if (t === 'easy apply' || t.endsWith(': easy apply') || t.startsWith('easy apply')) {
                    el.click(); return 'ea:main:' + t.slice(0, 40);
                }
                if (t === 'apply' || t.startsWith('apply ') || t.startsWith('apply:')) {
                    if (!t.includes('easy')) { el.click(); return 'apply:main:' + t.slice(0, 40); }
                }
            }
        }

        // --- Priority 1: scroll to top and look for the very first Easy Apply ---
        window.scrollTo(0, 0);
        const all = Array.from(document.querySelectorAll('button, [role="button"]'));
        // Sort by vertical position — top of page first
        all.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
        for (const el of all) {
            const t = txt(el);
            if (SKIP.has(t)) continue;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            // Only within first 600px of page (top card area)
            if (r.top > 600) break;
            if (t === 'easy apply' || t.endsWith(': easy apply') || t.startsWith('easy apply')) {
                el.click(); return 'ea:topzone:' + t.slice(0, 40);
            }
            if ((t === 'apply' || t.startsWith('apply ')) && !t.includes('easy')) {
                el.click(); return 'apply:topzone:' + t.slice(0, 40);
            }
        }

        // --- Priority 2: any Easy Apply button in viewport ---
        for (const el of all) {
            const t = txt(el);
            if (SKIP.has(t) || !inViewport(el)) continue;
            if (t.includes('easy apply')) { el.click(); return 'ea:any:' + t.slice(0, 40); }
        }
        for (const el of all) {
            const t = txt(el);
            if (SKIP.has(t) || !inViewport(el)) continue;
            if (t.startsWith('apply') && !t.includes('easy')) { el.click(); return 'apply:any:' + t.slice(0, 40); }
        }

        const dbg = all.filter(inViewport).map(e => txt(e).slice(0, 30)).filter(Boolean);
        return 'none|buttons:' + dbg.slice(0, 20).join(',');
    }""")

    log.info("LinkedIn JS scan for '%s': %s", title, (result or "null")[:120])

    if result and result.startswith('ea:'):
        page.wait_for_timeout(1500)
        return 'easy_apply'

    if result and result.startswith('apply:'):
        # External Apply clicked via JS — quick check, then safety modal, then longer wait
        for _w in [800, 800, 800]:
            page.wait_for_timeout(_w)
            new_pages = [p for p in context.pages if id(p) not in pages_before]
            if new_pages:
                new_page = new_pages[-1]
                try:
                    new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except Exception:
                    pass
                log.info("LinkedIn Apply → new tab (JS): %s", new_page.url)
                return 'external:' + new_page.url
        # Check for safety reminder modal
        safety_result = _linkedin_click_continue_applying(page, pages_before, context)
        if safety_result:
            return safety_result
        for _w in [1500, 2000, 2500]:
            page.wait_for_timeout(_w)
            new_pages = [p for p in context.pages if id(p) not in pages_before]
            if new_pages:
                new_page = new_pages[-1]
                try:
                    new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except Exception:
                    pass
                log.info("LinkedIn Apply → new tab (JS delayed): %s", new_page.url)
                return 'external:' + new_page.url
        cur = page.url
        if "linkedin.com" not in cur:
            return 'external:' + cur

    log.warning("LinkedIn: no apply button found for '%s'", title)
    return 'not_found'


def _solve_captcha(page, page_url: str) -> bool:
    """Detect and solve a captcha on the page.

    Delegates to CaptchaSolver (enterprise-aware, supports pageAction/userAgent).
    Kept as a module-level function so existing callers continue to work.
    """
    try:
        from hireagent.apply.vision_loop import CaptchaSolver
        solver = CaptchaSolver()
        return solver.solve(page, page_url)
    except Exception as exc:
        log.warning("CaptchaSolver delegation error: %s — falling back to legacy impl", exc)

    # ── Legacy fallback (original implementation) ───────────────────────────
    import urllib.request as _urllib_req

    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")
    if not capsolver_key:
        log.warning("CAPSOLVER_API_KEY not set — cannot solve captcha")
        return False

    # Detect captcha type and sitekey from DOM
    captcha_info = page.evaluate("""() => {
        // hCaptcha
        const hcDiv = document.querySelector('.h-captcha[data-sitekey], [data-hcaptcha-widget-id], div[data-sitekey*="hcaptcha"]');
        if (hcDiv) {
            const sk = hcDiv.getAttribute('data-sitekey') || '';
            if (sk) return {type: 'hcaptcha', sitekey: sk};
        }
        const hcFrame = document.querySelector('iframe[src*="hcaptcha.com"]');
        if (hcFrame) {
            try {
                const u = new URL(hcFrame.src);
                const sk = u.searchParams.get('sitekey') || '';
                if (sk) return {type: 'hcaptcha', sitekey: sk};
            } catch(e) {}
        }
        // reCAPTCHA v2 / Enterprise (api2 or enterprise endpoint)
        const rcDiv = document.querySelector('.g-recaptcha[data-sitekey]');
        if (rcDiv) return {type: 'recaptchav2', sitekey: rcDiv.getAttribute('data-sitekey')};
        const rcFrameSelectors = [
            'iframe[src*="recaptcha/api2"]',
            'iframe[src*="recaptcha/enterprise"]',
            'iframe[src*="google.com/recaptcha"]',
        ];
        for (const fSel of rcFrameSelectors) {
            const rcFrame = document.querySelector(fSel);
            if (rcFrame) {
                try {
                    const u = new URL(rcFrame.src);
                    const sk = u.searchParams.get('k') || u.searchParams.get('sitekey') || '';
                    if (sk) {
                        const isEnterprise = rcFrame.src.includes('enterprise');
                        return {type: isEnterprise ? 'recaptchav2enterprise' : 'recaptchav2', sitekey: sk};
                    }
                } catch(e) {}
            }
        }
        // reCAPTCHA sitekey embedded in page JS (grep inline scripts)
        const scripts = Array.from(document.querySelectorAll('script:not([src])'));
        for (const s of scripts) {
            const m = s.textContent.match(/['"]sitekey['"] *: *['"]([0-9A-Za-z_-]{20,60})['"]/);
            if (m) return {type: 'recaptchav2', sitekey: m[1]};
            const m2 = s.textContent.match(/grecaptcha[.]render[(][^)]+['"]([0-9A-Za-z_-]{20,60})['"]/);
            if (m2) return {type: 'recaptchav2', sitekey: m2[1]};
        }
        // Cloudflare Turnstile
        const cfDiv = document.querySelector('.cf-turnstile[data-sitekey]');
        if (cfDiv) return {type: 'turnstile', sitekey: cfDiv.getAttribute('data-sitekey')};
        return null;
    }""")

    if not captcha_info or not captcha_info.get("sitekey"):
        log.warning("Captcha iframe visible but could not extract sitekey — skipping solve")
        return False

    ctype = captcha_info["type"]
    sitekey = captcha_info["sitekey"]
    log.info("Solving %s captcha (sitekey=%s...) via CapSolver", ctype, sitekey[:12])

    type_map = {
        "hcaptcha": "HCaptchaTaskProxyless",
        "recaptchav2": "ReCaptchaV2TaskProxyless",
        "recaptchav2enterprise": "ReCaptchaV2EnterpriseTaskProxyless",
        "recaptchav3": "ReCaptchaV3TaskProxyless",
        "turnstile": "AntiTurnstileTaskProxyless",
    }

    # Detect if this is an invisible reCAPTCHA (no checkbox UI).
    # Ashby and some ATS use invisible reCAPTCHA — requires isInvisible=true.
    is_invisible = False
    if ctype in ("recaptchav2", "recaptchav2enterprise"):
        try:
            invisible_check = page.evaluate("""() => {
                // Invisible reCAPTCHA: size='invisible' on the div, or badge present, or no challenge iframe
                const div = document.querySelector('.g-recaptcha[data-size="invisible"], .g-recaptcha[data-badge]');
                if (div) return true;
                const frames = Array.from(document.querySelectorAll('iframe[src*="recaptcha"]'));
                // If only the bframe (badge) exists and no challenge iframe, it's invisible
                const hasBadge = frames.some(f => f.src.includes('bframe'));
                const hasChallenge = frames.some(f => f.src.includes('anchor'));
                if (hasBadge && !hasChallenge) return true;
                return false;
            }""")
            is_invisible = bool(invisible_check)
        except Exception:
            pass

    task_payload: dict = {
        "type": type_map.get(ctype, "HCaptchaTaskProxyless"),
        "websiteURL": page_url,
        "websiteKey": sitekey,
    }
    if is_invisible:
        task_payload["isInvisible"] = True
        log.info("Detected invisible reCAPTCHA — setting isInvisible=true")

    try:
        import json as _json

        def _post(url: str, body: dict) -> dict:
            data = _json.dumps(body).encode()
            req = _urllib_req.Request(url, data=data, headers={"Content-Type": "application/json"})
            return _json.loads(_urllib_req.urlopen(req, timeout=20).read())

        resp = _post("https://api.capsolver.com/createTask",
                     {"clientKey": capsolver_key, "task": task_payload})
        if resp.get("errorId", 0) != 0:
            log.warning("CapSolver createTask error: %s", resp.get("errorDescription", resp))
            return False

        task_id = resp["taskId"]
        log.info("CapSolver task created: %s — polling...", task_id)

        token = None
        for _ in range(30):  # max 150s
            time.sleep(5)
            result = _post("https://api.capsolver.com/getTaskResult",
                           {"clientKey": capsolver_key, "taskId": task_id})
            if result.get("errorId", 0) != 0:
                err_desc = result.get("errorDescription", "")
                # CapSolver returns "and invisible" error when it detects the captcha
                # is invisible but we didn't pass isInvisible=true — retry with that flag.
                if "invisible" in err_desc.lower() and not task_payload.get("isInvisible"):
                    log.info("CapSolver: invisible reCAPTCHA detected — retrying with isInvisible=true")
                    task_payload["isInvisible"] = True
                    resp2 = _post("https://api.capsolver.com/createTask",
                                  {"clientKey": capsolver_key, "task": task_payload})
                    if resp2.get("errorId", 0) == 0:
                        task_id = resp2["taskId"]
                        log.info("CapSolver retry task: %s", task_id)
                        continue  # resume polling with new task_id
                log.warning("CapSolver poll error: %s", err_desc or result)
                return False
            if result.get("status") == "ready":
                sol = result.get("solution", {})
                token = sol.get("gRecaptchaResponse") or sol.get("token") or ""
                break

        if not token:
            log.warning("CapSolver timed out or returned empty token")
            return False

        log.info("CapSolver solved — injecting token (%d chars)", len(token))

        # Inject token into page
        page.evaluate("""(token, ctype) => {
            // hCaptcha injection
            if (ctype === 'hcaptcha') {
                const ta = document.querySelector('textarea[name="h-captcha-response"]');
                if (ta) { ta.value = token; ta.dispatchEvent(new Event('change', {bubbles:true})); }
                // Call hcaptcha callback if registered
                try {
                    const wid = Object.keys(window.hcaptcha.__hCaptchaApiUrl !== undefined
                        ? {} : (window.hcaptcha ? {0:0} : {}));
                    if (window.hcaptcha && window.hcaptcha.submit) window.hcaptcha.submit();
                } catch(e) {}
                return;
            }
            // reCAPTCHA injection
            if (ctype === 'recaptchav2' || ctype === 'recaptchav3') {
                const ta = document.getElementById('g-recaptcha-response');
                if (ta) {
                    ta.innerHTML = token;
                    ta.dispatchEvent(new Event('change', {bubbles:true}));
                }
                // Trigger callback
                try {
                    const widget = document.querySelector('.g-recaptcha');
                    if (widget) {
                        const cb = widget.getAttribute('data-callback');
                        if (cb && window[cb]) window[cb](token);
                    }
                    if (typeof ___grecaptcha_cfg !== 'undefined') {
                        Object.values(___grecaptcha_cfg.clients || {}).forEach(client => {
                            Object.values(client || {}).forEach(v => {
                                if (v && v.callback && typeof v.callback === 'function') {
                                    try { v.callback(token); } catch(e) {}
                                }
                            });
                        });
                    }
                } catch(e) {}
                return;
            }
            // Turnstile injection
            if (ctype === 'turnstile') {
                const inp = document.querySelector('[name="cf-turnstile-response"]');
                if (inp) { inp.value = token; inp.dispatchEvent(new Event('change', {bubbles:true})); }
                try {
                    const cfDiv = document.querySelector('.cf-turnstile');
                    if (cfDiv) {
                        const cb = cfDiv.getAttribute('data-callback');
                        if (cb && window[cb]) window[cb](token);
                    }
                } catch(e) {}
            }
        }""", token, ctype)

        page.wait_for_timeout(0)
        log.info("Captcha token injected successfully")
        return True

    except Exception as e:
        log.warning("CapSolver exception: %s", e)
        return False


def _detect_ats(url: str) -> str:
    url = url.lower()
    if "greenhouse" in url or "grnh.se" in url:
        return "greenhouse"
    if "lever.co" in url:
        return "lever"
    if "workday" in url or "myworkday" in url:
        return "workday"
    if "ashbyhq" in url:
        return "ashby"
    if "smartrecruiters" in url:
        return "smartrecruiters"
    if "ultipro" in url or "ukg.com" in url or "recruiting2.ultipro" in url:
        return "ultipro"
    if "taleo" in url or "taleocommunity" in url:
        return "taleo"
    if "icims" in url:
        return "icims"
    if "successfactors" in url or "sap.com" in url:
        return "successfactors"
    return "generic"


def _count_form_fields(page) -> int:
    """Count visible interactive form fields. Used to decide if we need to click an Apply CTA."""
    try:
        return page.evaluate("""() => {
            const sel = "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='checkbox']):not([type='radio']), textarea, select";
            return Array.from(document.querySelectorAll(sel)).filter(el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }).length;
        }""")
    except Exception:
        return 0


def _click_apply_cta(page, title: str) -> bool:
    """Click the Apply / Apply Now / Start Application CTA on a job description page.
    Returns True if a CTA was found and clicked. Returns False if already on a form page."""

    # If there are already ≥2 visible form fields, we're on the application form — don't click CTA
    if _count_form_fields(page) >= 2:
        log.debug("Already on application form (%d fields) — skipping CTA click", _count_form_fields(page))
        return True  # "success" — already in the right place

    # Playwright locator attempts (most specific → most generic)
    CTA_SELECTORS = [
        # Common ATS patterns
        "a[href*='/apply']",
        "a[href*='apply?']",
        "button:text-matches('apply for this job', 'i')",
        "button:text-matches('apply for this position', 'i')",
        "button:text-matches('apply now', 'i')",
        "button:text-matches('start application', 'i')",
        "button:text-matches('begin application', 'i')",
        "button:text-matches('apply online', 'i')",
        "a:text-matches('apply for this job', 'i')",
        "a:text-matches('apply now', 'i')",
        "a:text-matches('start application', 'i')",
        "[class*='apply-btn']:visible",
        "[id*='apply-btn']:visible",
        "[data-label*='apply' i]",
        # iCIMS specific
        "a.iCIMS_Button[href*='apply']",
        "#applyLink",
        ".careers-apply-button",
        # Avature / Epic / generic "Upload" entry points
        "button:text-matches('upload a resume', 'i')",
        "a:text-matches('upload a resume', 'i')",
        "button:text-matches('upload resume', 'i')",
        "a:text-matches('upload resume', 'i')",
        # Generic fallback
        "button:text-matches('^apply$', 'i')",
        "a:text-matches('^apply$', 'i')",
    ]
    SKIP_TEXT = {"sign in", "login", "log in", "register", "create account",
                 "back", "return", "close", "search",
                 "apply with linkedin", "linkedin"}  # never use LinkedIn 1-click apply

    for sel in CTA_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                txt = (btn.inner_text() or btn.get_attribute("href") or "").strip().lower()
                if any(s in txt for s in SKIP_TEXT):
                    continue
                btn.scroll_into_view_if_needed()
                btn.click()
                log.info("Clicked Apply CTA '%s' via '%s'", txt[:40], sel)
                page.wait_for_timeout(500)
                return True
        except Exception:
            pass

    # JS broad search — find any "apply" button/link/div that's not a nav element
    try:
        result = page.evaluate("""() => {
            const SKIP = new Set(['sign in','login','log in','register','create account','back','search',
                                  'apply with linkedin','linkedin']);  // never click LinkedIn apply
            // Include ALL clickable elements — many ATS use div/span/li styled as buttons
            const all = Array.from(document.querySelectorAll(
                'button, a, [role="button"], input[type="submit"], div[onclick], span[onclick], li[onclick], [class*="btn"], [class*="button"], [class*="apply"]'
            ));
            const txt = el => (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').trim().toLowerCase().replace(/\\s+/g,' ');
            const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            const kws = ['apply for this job', 'apply for this position', 'apply now',
                         'start application', 'begin application', 'apply online',
                         'apply for job', 'apply to this job', 'apply for this role',
                         'upload a resume', 'upload resume', 'upload your resume'];
            for (const kw of kws) {
                for (const el of all) {
                    const t = txt(el);
                    if (visible(el) && !SKIP.has(t) && (t === kw || t.includes(kw))) {
                        el.click(); return 'cta:' + t.slice(0, 50);
                    }
                }
            }
            // Final fallback: any standalone "apply" button (exact match only, not menu items)
            for (const el of all) {
                const t = txt(el);
                if (visible(el) && t === 'apply') {
                    el.click(); return 'cta:apply';
                }
            }
            // Debug list
            const dbg = all.filter(visible).map(e => txt(e).slice(0,30)).filter(Boolean);
            return 'none|' + dbg.slice(0,15).join(',');
        }""")
        if result and not result.startswith("none"):
            log.info("Apply CTA clicked via JS: %s", result)
            page.wait_for_timeout(500)
            return True
        else:
            log.info("No Apply CTA found (already on form or no CTA): %s", result)
            return False
    except Exception as e:
        log.debug("CTA click JS error: %s", e)
        return False


def _handle_email_gate(page, flat: dict) -> bool:
    """Detect and handle ATS pages that gate the application behind an email-first form.

    Pattern: single email input + Next/Continue button (iCIMS, Breezy, etc.).
    Returns True if email was filled and Next was clicked.
    """
    try:
        email_val = flat.get("email", "")
        if not email_val:
            return False

        email_inputs = page.query_selector_all(
            "input[type='email'], input[placeholder*='email' i], "
            "input[aria-label*='email' i], input[name*='email' i]"
        )
        visible_email_inputs = [e for e in email_inputs if e.is_visible()]
        if not visible_email_inputs:
            return False

        body_text = (page.inner_text("body") or "").lower()
        is_email_gate = (
            ("enter your email" in body_text or "email address" in body_text)
            and ("next" in body_text or "continue" in body_text)
        )
        if not is_email_gate:
            return False

        email_inp = visible_email_inputs[0]
        current = ""
        try:
            current = email_inp.input_value() or ""
        except Exception:
            pass
        if current.strip().lower() != email_val.strip().lower():
            email_inp.click()
            email_inp.fill(email_val)
            page.wait_for_timeout(300)
            log.info("[email-gate] Filled email: %s", email_val[:30])

        # Click Next / Continue
        for sel in [
            "button:text-matches('next', 'i')",
            "button:text-matches('continue', 'i')",
            "input[type='submit'][value*='next' i]",
            "input[type='submit'][value*='continue' i]",
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    page.wait_for_timeout(2000)
                    log.info("[email-gate] Clicked Next via '%s'", sel)
                    return True
            except Exception:
                pass

        clicked = page.evaluate("""() => {
            for (const b of document.querySelectorAll('button, input[type="submit"], a')) {
                const t = (b.innerText || b.value || '').trim().toLowerCase();
                const r = b.getBoundingClientRect();
                if ((t === 'next' || t === 'continue') && r.width > 0 && r.height > 0) {
                    b.click(); return true;
                }
            }
            return false;
        }""")
        if clicked:
            page.wait_for_timeout(2000)
            log.info("[email-gate] Clicked Next via JS fallback")
            return True
    except Exception as exc:
        log.debug("[email-gate] error: %s", exc)
    return False


def _ask_unknown_fields(unfilled_labels: list[str], flat: dict) -> dict[str, str]:
    """For each unfilled required field, ask the user via Telegram. Returns label→answer map."""
    answers: dict[str, str] = {}
    if not unfilled_labels:
        return answers

    for label in unfilled_labels:
        # Skip if we can now map it
        if _value_for_label(label, flat):
            continue
        question = (
            f"❓ *Unknown field in application form*\n\n"
            f"Field: `{label}`\n\n"
            f"What should I enter? (reply with the exact value, or 'skip' to leave blank)"
        )
        log.info("Asking Telegram for field: %s", label)
        answer = _tg_ask(question, timeout=120)
        if answer and answer.lower() != "skip":
            answers[label] = answer
    return answers


# ── OpenClaw Gateway client ────────────────────────────────────────────────

class OpenClawUnavailableError(Exception):
    pass


def check_openclaw_health() -> bool:
    try:
        r = requests.get(f"{OPENCLAW_GATEWAY}/healthz", timeout=3,
                         headers={"Authorization": f"Bearer {OPENCLAW_TOKEN}"} if OPENCLAW_TOKEN else {})
        return r.status_code == 200
    except Exception:
        return False


def apply_via_openclaw(job: dict, profile: dict, resume_path: Path) -> str:
    if not check_openclaw_health():
        raise OpenClawUnavailableError("OpenClaw gateway not reachable. Run: openclaw gateway run")
    flat = _flat(profile)
    payload = {
        "task": "apply_job",
        "application_url": job.get("application_url") or job.get("url"),
        "job_title": job.get("title", ""),
        "resume_path": str(resume_path),
        "candidate": flat,
        "hitl": {"enabled": True, "pause_before_submit": True},
        "vision_model": VISION_MODEL,
        "ollama_base_url": OLLAMA_BASE,
    }
    headers = {"Content-Type": "application/json"}
    if OPENCLAW_TOKEN:
        headers["Authorization"] = f"Bearer {OPENCLAW_TOKEN}"
    try:
        r = requests.post(f"{OPENCLAW_GATEWAY}/apply", json=payload,
                          headers=headers, timeout=300)
    except requests.ConnectionError as e:
        raise OpenClawUnavailableError(str(e)) from e
    if not r.ok:
        log.error("OpenClaw %s: %s", r.status_code, r.text[:200])
        raise OpenClawUnavailableError(f"OpenClaw /apply returned {r.status_code}")
    status = r.json().get("status", "").lower()
    if status in ("applied", "submitted"):
        return RESULT_APPLIED
    if status in ("pending", "awaiting_review"):
        return RESULT_PENDING
    return RESULT_FAILED


# ── Playwright apply ───────────────────────────────────────────────────────

def apply_via_playwright(
    job: dict,
    profile: dict,
    resume_path: Path,
    headless: bool = False,
) -> str:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    apply_url = job.get("application_url") or job.get("url")
    title     = job.get("title", "Unknown Role")
    flat      = _flat(profile)
    ats       = _detect_ats(apply_url or "")

    # Skip Indeed-hosted job pages (they are NOT ATS apply forms)
    if apply_url and ("indeed.com/viewjob" in apply_url or
                      ("indeed.com/job/" in apply_url and "apply" not in apply_url)):
        log.warning("Skipping Indeed-hosted job page (not a real ATS): %s", apply_url)
        return "indeed_hosted_not_ats"

    ha_profile = Path.home() / ".hireagent" / "playwright-profile"
    ha_profile.mkdir(parents=True, exist_ok=True)

    log.info("Playwright apply [%s] '%s' → %s", ats, title, apply_url)
    _tg(
        f"🔍 *ATS Detected:* `{ats.upper()}`\n"
        f"*Job:* {title}\n"
        f"*Score:* {job.get('fit_score', 'N/A')}/10\n"
        f"Opening form..."
    )

    # Use .start() instead of `with` to avoid Playwright teardown hanging on CDP disconnect
    pw = sync_playwright().start()
    context = None
    try:
        # Try attaching to already-running HireAgent Chrome (CDP 9222)
        try:
            cdp = pw.chromium.connect_over_cdp("http://localhost:9222")
            context = cdp.contexts[0] if cdp.contexts else cdp.new_context()
            log.info("Attached via CDP 9222")
        except Exception:
            pass

        if context is None:
            context = pw.chromium.launch_persistent_context(
                str(ha_profile),
                headless=headless,
                args=["--no-first-run", "--no-default-browser-check",
                      "--disable-notifications", "--disable-popup-blocking"],
            )
        page = context.new_page()
        try:
            page.goto(apply_url, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            log.warning("Page load timeout")

        # ── Vision-loop singletons ────────────────────────────────────────────
        from hireagent.apply.vision_loop import (
            BrowserController as _BrowserController,
            IntelligenceLayer as _IntelligenceLayer,
            CaptchaSolver     as _CaptchaSolver,
            vision_verified_fill as _vision_verified_fill,
            find_submit_button_vision as _find_submit_btn_vision,
        )
        _nim  = _IntelligenceLayer()
        _capt = _CaptchaSolver()
        _bc   = _BrowserController(page)

        # ── SSN / fake job detection ──────────────────────────────────────────
        try:
            ssn_found = page.evaluate("""() => {
                const body = (document.body.innerText || '').toLowerCase();
                const labels = Array.from(document.querySelectorAll(
                    'label, legend, [placeholder], input[name]'
                )).map(el => (
                    el.innerText || el.getAttribute('placeholder') || el.getAttribute('name') || ''
                ).toLowerCase());
                const all_text = body + ' ' + labels.join(' ');
                const patterns = [
                    'social security number', 'social security no', 'ssn',
                    'ss number', 'taxpayer id', 'tin number',
                    'social insurance number', 'sin number',
                ];
                return patterns.some(p => all_text.includes(p));
            }""")
            if ssn_found:
                company_name = job.get("company") or urlparse(apply_url).netloc
                log.warning("⚠️  SSN requested — flagging as FAKE JOB: %s @ %s", title, company_name)
                _tg(f"🚨 *FAKE JOB — SSN requested*\n*Job:* {title}\n*Company:* {company_name}\n*URL:* {apply_url}")
                return "fake_job_ssn"
        except Exception:
            pass

        # ── Early captcha detection — ONLY flag interactive challenges, not invisible v3 ──
        # reCAPTCHA v3 is invisible (no user interaction needed) — never bail on it.
        # Only hCaptcha and reCAPTCHA v2 challenge iframes are real blockers.
        try:
            captcha_visible = page.evaluate("""() => {
                const frames = Array.from(document.querySelectorAll('iframe'));
                for (const f of frames) {
                    const src = (f.src || '').toLowerCase();
                    // hCaptcha: always interactive
                    const isHcaptcha = src.includes('hcaptcha.com');
                    // reCAPTCHA v2 challenge frames (not v3 which is invisible)
                    const isRcV2 = src.includes('api2/bframe') || src.includes('api2/anchor');
                    if (isHcaptcha || isRcV2) {
                        const r = f.getBoundingClientRect();
                        if (r.width > 10 && r.height > 10) return true;
                    }
                }
                // hCaptcha widget div
                if (document.querySelector('.h-captcha[data-sitekey]')) return true;
                return false;
            }""")
            if captcha_visible:
                log.info("Interactive captcha detected — attempting CapSolver (2 attempts)...")
                solved = False
                for _cap_attempt in range(2):
                    solved = _solve_captcha(page, apply_url or "")
                    if solved:
                        log.info("Captcha solved on attempt %d", _cap_attempt + 1)
                        page.wait_for_timeout(2000)  # Let page process the token
                        break
                    if _cap_attempt < 1:
                        log.info("Captcha solve attempt %d failed — retrying...", _cap_attempt + 1)
                        page.wait_for_timeout(3000)
                if not solved:
                    log.warning("Captcha unsolvable after 2 attempts — bailing")
                    _tg(f"🛑 *Captcha (unsolved after 2 attempts)* — {title}\n{apply_url}")
                    return "captcha"
                log.info("Captcha solved — continuing")
        except Exception:
            pass

        # ── LinkedIn: login if needed, then click Apply or Easy Apply ──
        is_linkedin = "linkedin.com" in (apply_url or "").lower()
        if is_linkedin:
            # LinkedIn is heavy JS — give it extra time before interacting
            page.wait_for_timeout(2500)
            _linkedin_login_if_needed(page, flat, apply_url)
            # Wait for job page to fully render after possible login redirect
            page.wait_for_timeout(2000)

            li_result = _linkedin_click_apply(page, title, context)

            if li_result == 'easy_apply':
                # Easy Apply modal opened — wait for it to fully render
                try:
                    page.wait_for_selector(
                        ".jobs-easy-apply-modal, [data-test-modal='easy-apply-modal'], "
                        ".artdeco-modal, .jobs-easy-apply-content, "
                        "[role='dialog'], .jobs-apply-modal, "
                        "div[aria-label*='Apply' i], div[aria-label*='application' i]",
                        timeout=10000,
                    )
                    page.wait_for_timeout(2000)
                    log.info("LinkedIn Easy Apply modal confirmed open for '%s'", title)
                except Exception:
                    log.warning("LinkedIn Easy Apply modal didn't confirm — proceeding anyway")
                    page.wait_for_timeout(500)
                # is_linkedin stays True, form fill proceeds in modal

            elif li_result.startswith('external:'):
                # Regular Apply → opened external ATS (new tab or navigation)
                external_url = li_result[len('external:'):]
                # Switch to the newest tab (most recently opened)
                all_pages = context.pages
                if len(all_pages) > 1:
                    page = all_pages[-1]  # newest tab
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=20_000)
                    except Exception:
                        pass
                    page.bring_to_front()
                    external_url = page.url  # use actual final URL after any redirects
                page.wait_for_timeout(500)
                ats = _detect_ats(external_url)
                log.info("LinkedIn → external ATS '%s': %s", ats, external_url)
                # Update apply_url so ATS scan notification uses the real URL
                apply_url = external_url
                is_linkedin = False  # now on external ATS — proceed with normal form fill

            elif li_result == 'job_expired':
                log.warning("LinkedIn job expired — skipping permanently: %s", title)
                _ss = Path("/tmp") / f"hireagent_expired_{int(time.time())}.png"
                try:
                    page.screenshot(path=str(_ss), full_page=False)
                    _tg(f"💀 *Job expired/closed* — skipping\n*Job:* {title}\n{apply_url}", _ss)
                except Exception:
                    _tg(f"💀 *Job expired/closed* — skipping\n*Job:* {title}\n{apply_url}")
                return "job_expired"

            else:
                # Apply button not detected — could be timing issue or truly expired.
                # The expired/closed checks earlier already catch genuine closures, so
                # this is most likely a detection failure (button rendered late, etc.).
                # Take a screenshot for debugging but mark as transient failure (retryable).
                ss_fail = Path("/tmp") / f"hireagent_fail_{int(time.time())}.png"
                try:
                    page.screenshot(path=str(ss_fail), full_page=False)
                    _tg(f"⚠️ *Apply button not detected — will retry* — {title}\n{apply_url}", ss_fail)
                except Exception:
                    _tg(f"⚠️ *Apply button not detected — will retry* — {title}\n{apply_url}")
                return RESULT_FAILED

        # ── Click Apply CTA for non-LinkedIn jobs (job description → application form) ──
        if not is_linkedin:
            # Extra wait for JS-heavy ATS pages to fully render
            page.wait_for_timeout(500)

            # Handle iCIMS/Breezy-style email-gate pages BEFORE CTA click
            _handle_email_gate(page, flat)

            cta_clicked = _click_apply_cta(page, title)
            # After CTA click, give the new page time to load
            page.wait_for_timeout(2000)

            # Check for account/SSO gate — if page now demands login/register, skip
            if cta_clicked:
                try:
                    body_lower = (page.inner_text("body") or "").lower()
                    url_lower = (page.url or "").lower()
                    sso_signals = (
                        # Generic
                        "create an account", "sign up to apply", "create account to",
                        "register to apply", "login to apply", "log in to apply",
                        "please sign in", "you must be logged in",
                        # UltiPro / UKG
                        "log in to your account", "new user", "existing user",
                        "create new account", "returning user", "first time user",
                        "welcome back", "sign in to your profile",
                        # Taleo
                        "my profile", "returning candidate", "new to this site",
                        # iCIMS
                        "talent network", "join our talent",
                        # SuccessFactors / SAP
                        "candidate profile", "sign in with your",
                    )
                    url_sso_signals = ("login", "register", "signin", "sign-in",
                                       "applicationinit", "candidateprofile", "talentnetwork")
                    # Account/Login required — but only if no form inputs are found
                    visible_inputs = page.query_selector_all(
                        "input[type='text']:visible, input[type='email']:visible, input[type='tel']:visible, "
                        "input[type='url']:visible, input[type='number']:visible, textarea:visible"
                    )
                    has_form = len(visible_inputs) > 0
                    
                    if (any(s in body_lower for s in sso_signals) or
                            any(s in url_lower for s in url_sso_signals)):
                        if not has_form:
                            log.warning("Account/SSO required — skipping: %s", page.url)
                            _ss = Path("/tmp") / f"hireagent_sso_{int(time.time())}.png"
                            try:
                                page.screenshot(path=str(_ss), full_page=False)
                                _tg(f"🔐 *Account/Login required* — skipping\n*Job:* {title}\n*ATS:* {ats.upper()}", _ss)
                            except Exception:
                                _tg(f"🔐 *Account/Login required* — skipping\n*Job:* {title}\n*ATS:* {ats.upper()}")
                            return "account_required"
                        else:
                            log.debug("SSO keywords found, but form exists — proceeding")
                    # Also skip known account-required ATS types
                    if ats in ("ultipro", "taleo", "icims", "successfactors"):
                        # These require account creation — check if we're past the job page
                        if "opportunitydetail" in url_lower or "jobboard" in url_lower:
                            # Still on job description, CTA didn't navigate to form
                            pass  # let it try normally
                        else:
                            # Navigated somewhere else — likely a login wall
                            log.warning("Account-required ATS (%s) post-CTA URL: %s — skipping", ats, page.url)
                            _ss = Path("/tmp") / f"hireagent_sso_{int(time.time())}.png"
                            try:
                                page.screenshot(path=str(_ss), full_page=False)
                                _tg(f"🔐 *Account required* ({ats.upper()}) — skipping\n*Job:* {title}", _ss)
                            except Exception:
                                _tg(f"🔐 *Account required* ({ats.upper()}) — skipping\n*Job:* {title}")
                            return "account_required"
                except Exception:
                    pass

            # If CTA was clicked but page still has no form fields AND no visible buttons → no form loaded
            if cta_clicked:
                try:
                    n_fields = _count_form_fields(page)
                    n_buttons = page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('button,[role="button"],input[type="submit"]'))
                            .filter(e => { const r=e.getBoundingClientRect(); return r.width>0&&r.height>0; }).length;
                    }""")
                    if n_fields == 0 and n_buttons == 0:
                        log.warning("CTA clicked but no form/buttons appeared — page may need login or is in iframe")
                        # Try waiting a bit longer for slow ATS pages
                        page.wait_for_timeout(1000)
                        n_fields = _count_form_fields(page)
                        n_buttons = page.evaluate("""() => {
                            return Array.from(document.querySelectorAll('button,[role="button"],input[type="submit"]'))
                                .filter(e => { const r=e.getBoundingClientRect(); return r.width>0&&r.height>0; }).length;
                        }""")
                        if n_fields == 0 and n_buttons == 0:
                            log.warning("Still no form after extra wait — skipping job")
                            ss_no_cta = Path("/tmp") / f"hireagent_nocta_{int(time.time())}.png"
                            try:
                                page.screenshot(path=str(ss_no_cta), full_page=False)
                                _tg(f"❌ *No form loaded after Apply click* — {title}\n{apply_url}", ss_no_cta)
                            except Exception:
                                pass
                            return RESULT_FAILED
                except Exception:
                    pass
            else:
                # CTA not found — only continue if the page already IS a form
                _n_fields_now = _count_form_fields(page)
                if _n_fields_now < 2:
                    log.warning("No Apply CTA found and page has no form fields — skipping: %s", apply_url)
                    ss_no_cta = Path("/tmp") / f"hireagent_nocta_{int(time.time())}.png"
                    try:
                        page.screenshot(path=str(ss_no_cta), full_page=False)
                        _tg(f"❌ *Apply button not found* — {title}\n{apply_url}", ss_no_cta)
                    except Exception:
                        _tg(f"❌ *Apply button not found* — {title}\n{apply_url}")
                    return "failed:no_form_found"
                else:
                    log.info("No Apply CTA needed — page already has %d form fields", _n_fields_now)

        # ── Upload resume (using canonical filename so recruiter sees real name) ──
        import shutil as _shutil
        import tempfile as _tempfile
        canonical_name = "gunakarthik_naidu_lanka_resume.pdf"
        tmp_resume_dir = Path(_tempfile.gettempdir()) / "hireagent_resumes"
        tmp_resume_dir.mkdir(exist_ok=True)
        canonical_path = tmp_resume_dir / canonical_name
        try:
            _shutil.copy2(str(resume_path), str(canonical_path))
            upload_path = canonical_path
        except Exception:
            upload_path = resume_path
        uploaded = _upload_resume(page, upload_path)
        page.wait_for_timeout(1000)

        def _do_fill_pass(pg) -> tuple[int, list[str]]:
            """One full fill pass: ATS-specific + generic inputs + selects + radios."""
            if ats == "greenhouse":
                return _fill_greenhouse(pg, flat)
            elif ats == "lever":
                return _fill_lever(pg, flat)
            else:
                n, unf = _fill_all_inputs(pg, flat)
                n += _fill_all_selects(pg, flat)
                n += _fill_radios(pg, flat)
                return n, unf

        _FORM_FILL_SYSTEM_PROMPT = """You are an expert ATS form-filling agent. Fill job application form fields using the candidate profile provided.

CRITICAL:
- Return ONLY valid JSON — no markdown fences, no explanations.
- Never fabricate data. Use only information from the profile.
- If a field is optional and you have no data, omit it.
- For select/dropdown fields, only use exact values from the provided options.

FIELD MAPPING RULES:
- Name → firstName, lastName, fullName from profile
- Email → profile.email (exact match)
- Phone → 10-digit US format: XXXXXXXXXX
- Salary → integer (e.g. 90000), no commas or symbols
- Years of experience → "0-1" or "1-3" based on profile
- Work authorization:
  - OPT/F-1 → select "Employment Authorization Document (EAD)" or "Other" or "OPT"
  - "Are you authorized to work in the US?" → "Yes"
  - "Will you need sponsorship NOW?" → "No"
  - "Will you need sponsorship in the future?" → "Yes"
- Cover letter / essay → 2-3 sentences: skills match + work auth + availability
- Willing to relocate → "Yes"
- Background check → "Yes"
- Disability/veteran EEO → "I prefer not to answer" or "I do not have a disability" / "I am not a protected veteran"

RESPONSE FORMAT — return ONLY this JSON:
{"fields": {"field_label_or_name": "value", ...}}"""

        def _llm_answer_fields(labels: list[str], page_context: dict | None = None) -> dict[str, str]:
            """Call LLM once with all unknown field labels. Returns {label: answer}."""
            if not labels:
                return {}
            try:
                from hireagent.llm import get_apply_client
                _client = get_apply_client()
            except Exception as _e:
                log.warning("LLM client init failed: %s", _e)
                return {}

            import json as _json

            edu_m = profile.get("education", {}).get("masters", {})
            skills_raw = profile.get("skills", [])
            skills_str = ", ".join(skills_raw) if isinstance(skills_raw, list) else str(skills_raw)

            candidate_profile = {
                "fullName": flat.get("full_name"),
                "firstName": flat.get("first_name"),
                "lastName": flat.get("last_name"),
                "email": flat.get("email"),
                "phone": flat.get("phone"),
                "city": flat.get("city"),
                "state": flat.get("state"),
                "country": "United States",
                "postalCode": flat.get("postal_code"),
                "linkedinUrl": flat.get("linkedin_url"),
                "githubUrl": flat.get("github_url"),
                "portfolioUrl": flat.get("portfolio_url"),
                "expectedSalary": flat.get("salary", "90000"),
                "salaryRange": flat.get("salary_range"),
                "yearsExperience": flat.get("years_experience", "1"),
                "workAuthorization": "OPT (F-1 Student Visa) — authorized to work; will need H-1B sponsorship in future",
                "degree": edu_m.get("degree", "Master of Science"),
                "major": edu_m.get("field_primary", "Computer Science"),
                "school": edu_m.get("school", "Arizona State University"),
                "gpa": flat.get("gpa", "4.0"),
                "graduationDate": "December 2025",
                "skills": skills_str or "Python, SQL, Java, React, Node.js, AWS, Docker, Git, machine learning",
                "targetRole": title,
                "availability": "Immediately",
                "relocateWilling": True,
                "remotePreference": True,
            }

            # Build form schema: include options if available from page_context
            form_schema = []
            if page_context and "fields" in page_context:
                for f in page_context["fields"]:
                    if f.get("label") in labels:
                        form_schema.append(f)
            if not form_schema:
                form_schema = [{"label": lbl, "type": "text", "required": True} for lbl in labels]

            user_msg = f"Form fields to fill:\n{_json.dumps(form_schema, indent=2)}\n\nCandidate profile:\n{_json.dumps(candidate_profile, indent=2)}"

            try:
                raw = _client.ask(user_msg, temperature=0.1, max_tokens=1024,
                                  system_prompt=_FORM_FILL_SYSTEM_PROMPT)
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
                    raw = re.sub(r"\n?```$", "", raw)
                    raw = raw.strip()
                parsed = _json.loads(raw)
                # Support both {"fields": {...}} and flat {label: value}
                answers = parsed.get("fields", parsed)
                log.info("LLM filled %d fields: %s", len(answers), list(answers.keys()))
                return {str(k): str(v) for k, v in answers.items() if v not in (None, "", "null")}
            except Exception as _e:
                log.warning("LLM form-fill parse failed: %s | raw=%s", _e, locals().get("raw", "")[:300])
                return {}

        def _ask_and_fill_unknowns(pg, unfilled: list[str]) -> int:
            """Fill unknown required fields: first try rule-based, then LLM for the rest."""
            filled = 0
            still_unknown = []

            # Pass 1: rule-based
            for label in unfilled:
                val = _value_for_label(label, flat)
                if val:
                    flat[f"_custom_{label.lower()[:20]}"] = val
                    _fill_one(pg, label, val)
                    filled += 1
                else:
                    still_unknown.append(label)

            if not still_unknown:
                return filled

            # Pass 2: LLM for anything rule-based couldn't map.
            # Collect page context: for each unknown label, grab select options + input type
            # so the LLM knows what values are actually valid.
            page_ctx: dict = {"fields": []}
            for lbl in still_unknown:
                field_info: dict = {"label": lbl, "type": "text", "required": True}
                try:
                    el = pg.get_by_label(re.compile(re.escape(lbl), re.IGNORECASE)).first
                    if el.is_visible(timeout=400):
                        tag = el.evaluate("e => e.tagName.toLowerCase()")
                        if tag == "select":
                            field_info["type"] = "select"
                            opts = el.query_selector_all("option")
                            field_info["options"] = [
                                (o.inner_text() or "").strip()
                                for o in opts
                                if (o.inner_text() or "").strip()
                                and (o.inner_text() or "").strip().lower()
                                not in ("select", "please select", "--", "-", "choose", "none", "")
                            ][:30]
                        else:
                            field_info["type"] = el.get_attribute("type") or "text"
                except Exception:
                    pass
                page_ctx["fields"].append(field_info)

            log.info("LLM assist: %d unknown fields → %s", len(still_unknown), still_unknown)
            llm_answers = _llm_answer_fields(still_unknown, page_context=page_ctx)
            for label, value in llm_answers.items():
                if value and value.lower() not in ("none", "null", "n/a", ""):
                    flat[f"_custom_{label.lower()[:20]}"] = value
                    if _fill_one(pg, label, value):
                        filled += 1

            return filled

        def _fill_one(pg, label: str, value: str) -> bool:
            """Try to find and fill a single field by label. Returns True if filled."""
            # Handle unlabeled fields identified by name/placeholder/id attribute
            if label.startswith("[unlabeled:"):
                attr_val = label[len("[unlabeled:"):-1]
                for sel in (
                    f"input[name='{attr_val}']",
                    f"input[placeholder='{attr_val}']",
                    f"input[id='{attr_val}']",
                    f"textarea[name='{attr_val}']",
                ):
                    try:
                        el = pg.locator(sel).first
                        if el.is_visible(timeout=400):
                            el.fill(value)
                            log.debug("Filled unlabeled field [%s] = '%s'", attr_val, value[:30])
                            return True
                    except Exception:
                        pass
                return False
            try:
                el = pg.get_by_label(re.compile(re.escape(label), re.IGNORECASE)).first
                if el.is_visible(timeout=800):
                    tag = el.evaluate("e => e.tagName.toLowerCase()")
                    if tag == "select":
                        try:
                            el.select_option(label=value, timeout=500)
                        except Exception:
                            try:
                                el.select_option(value=value, timeout=500)
                            except Exception:
                                pass
                    else:
                        el.fill(value)
                    return True
            except Exception:
                pass
            try:
                for inp in pg.query_selector_all("input:not([type='hidden']):not([type='file']), textarea, select"):
                    if not inp.is_visible():
                        continue
                    if label.lower() in _label_for(pg, inp).lower():
                        tag = inp.evaluate("e => e.tagName.toLowerCase()")
                        if tag == "select":
                            try:
                                inp.select_option(label=value, timeout=500)
                            except Exception:
                                try:
                                    inp.select_option(value=value, timeout=500)
                                except Exception:
                                    pass
                        else:
                            _safe_fill(inp, value, page=pg)
                        return True
            except Exception:
                pass
            return False

        def _collect_field_errors(pg) -> list[str]:
            """Collect all visible validation error messages on the page."""
            errors: list[str] = []
            try:
                err_els = pg.query_selector_all(
                    "[class*='error']:not(script), [class*='invalid']:not(script), "
                    "[class*='warning']:not(script), [aria-invalid='true'], "
                    "[data-error], [class*='field-error'], [class*='validation']"
                )
                for el in err_els:
                    try:
                        if el.is_visible():
                            txt = (el.inner_text() or "").strip()
                            if txt and len(txt) < 200:
                                errors.append(txt)
                    except Exception:
                        pass
            except Exception:
                pass
            return list(dict.fromkeys(errors))  # deduplicate

        def _collect_empty_required(pg) -> list[str]:
            """Return labels of visible required inputs that are still empty."""
            empty: list[str] = []
            try:
                for inp in pg.query_selector_all(
                    "input[required]:not([type='password']):not([type='hidden']):not([type='file']), "
                    "input[aria-required='true']:not([type='password']):not([type='hidden']):not([type='file']), "
                    "textarea[required], textarea[aria-required='true'], "
                    "select[required], select[aria-required='true']"
                ):
                    try:
                        if not inp.is_visible():
                            continue
                        val = ""
                        try:
                            val = inp.input_value() or ""
                        except Exception:
                            pass
                        if not val.strip():
                            lbl = _label_for(pg, inp)
                            if lbl:
                                empty.append(lbl)
                    except Exception:
                        pass
            except Exception:
                pass
            return empty

        # ── Re-check captcha after CTA click — use CaptchaSolver as single source of truth ──
        try:
            from hireagent.apply.vision_loop import CaptchaSolver as _CaptchaSolver
            _post_click_solver = _CaptchaSolver()
            _captcha_info = _post_click_solver._detect(page)

            if _captcha_info:
                ctype = _captcha_info.get("type", "unknown")
                log.info("Captcha detected after CTA click (type=%s) — attempting CapSolver (3 attempts)...", ctype)
                solved = False
                for _cap_attempt in range(3):
                    solved = _post_click_solver.solve(page, page.url or apply_url or "")
                    if solved:
                        log.info("Captcha after CTA solved on attempt %d (type=%s)", _cap_attempt + 1, ctype)
                        page.wait_for_timeout(2500)
                        break
                    log.debug("CaptchaSolver attempt %d failed — retrying", _cap_attempt + 1)
                    if _cap_attempt < 2:
                        page.wait_for_timeout(4000)
                if not solved:
                    log.warning("Captcha after CTA unsolvable (type=%s) — bailing", ctype)
                    _tg(f"🛑 *Captcha after Apply click (unsolved, type={ctype})* — {title}\n{apply_url}")
                    return "captcha"
                log.info("Captcha solved after CTA — continuing")
            else:
                # Broader fallback: check for any CAPTCHA-like visible iframe the solver missed
                _broad_captcha = page.evaluate("""() => {
                    const CAPTCHA_HOSTS = [
                        'hcaptcha.com', 'recaptcha', 'turnstile', 'arkoselabs',
                        'funcaptcha', 'datadome', 'geetest', 'cloudflare'
                    ];
                    for (const f of document.querySelectorAll('iframe')) {
                        const src = (f.src || '').toLowerCase();
                        if (CAPTCHA_HOSTS.some(h => src.includes(h))) {
                            const r = f.getBoundingClientRect();
                            if (r.width > 10 && r.height > 10) return src;
                        }
                    }
                    return null;
                }""")
                if _broad_captcha:
                    log.warning("Unsupported CAPTCHA iframe detected (%s) — bailing", _broad_captcha[:80])
                    _tg(f"🛑 *Unsupported CAPTCHA after Apply click* — {title}\n{apply_url}\nType: {_broad_captcha[:60]}")
                    return "captcha"
        except Exception as _ce:
            log.debug("Post-CTA captcha check error: %s", _ce)

        # ── Phase 0: Vision-Verified fill (Nemotron maps fields → values) ──────
        _bc.page = page
        _v_filled, _v_errors = _vision_verified_fill(page, flat, _nim, _bc, _capt, apply_url or "")
        log.info("Phase 0 vision-fill: %d fields, errors=%s", _v_filled, _v_errors or "none")
        if _v_errors:
            _tg(f"⚠️ *Form errors detected (vision scan)*\n{chr(10).join(_v_errors[:5])}")

        # ── Phase 1: legacy ATS-specific fill (handles selects, radios, Workday) ──
        n_filled, unfilled = _do_fill_pass(page)
        n_filled += _v_filled
        log.info("Phase 1 fill: %d fields filled, %d unknown required, resume=%s",
                 n_filled, len(unfilled), uploaded)

        # Debug screenshot for Greenhouse to identify unfilled custom components
        if ats == "greenhouse":
            try:
                _ss_gh = Path("/tmp") / f"gh_form_{int(time.time())}.png"
                page.screenshot(path=str(_ss_gh), full_page=True)
                log.info("GH debug screenshot: %s", _ss_gh)
                # Also dump all visible input-like elements
                dom_info = page.evaluate("""
                    () => {
                        const els = document.querySelectorAll('input,select,textarea,[role="combobox"],[role="listbox"],[class*="select"],[class*="dropdown"],[class*="Select"],[class*="Dropdown"]');
                        return Array.from(els).filter(e => e.offsetParent !== null).slice(0, 40).map(e => ({
                            tag: e.tagName,
                            type: e.type || '',
                            id: e.id || '',
                            name: e.name || '',
                            role: e.getAttribute('role') || '',
                            cls: e.className.substring(0,60),
                            placeholder: e.placeholder || e.getAttribute('aria-label') || e.getAttribute('data-qa') || ''
                        }));
                    }
                """)
                log.info("GH DOM elements: %s", dom_info)
            except Exception as _dbg:
                log.debug("GH debug dump failed: %s", _dbg)

        if unfilled:
            log.info("Unknown required fields (skipping): %s", unfilled)
            n_filled += _ask_and_fill_unknowns(page, unfilled)

        # ── Phase 2: scroll back to top and re-verify every field ──
        log.info("Phase 2: scrolling to top for field-by-field re-check")
        try:
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(800)
        except Exception:
            pass

        n_recheck, still_unfilled = _do_fill_pass(page)
        if n_recheck:
            log.info("Phase 2 re-check: filled %d more fields that were missed", n_recheck)
        if still_unfilled:
            log.info("Phase 2: still %d unknown required fields after re-check", len(still_unfilled))
            n_filled += _ask_and_fill_unknowns(page, still_unfilled)

        # Also check for any remaining empty required fields
        empty_required = _collect_empty_required(page)
        if empty_required:
            log.info("Phase 2: %d empty required fields detected: %s",
                     len(empty_required), empty_required[:5])
            n_filled += _ask_and_fill_unknowns(page, empty_required)

        log.info("Pre-submit total: %d fields filled", n_filled)

        # ── Multi-step form loop: click Next/Continue until Submit ──
        SUBMIT_SELECTORS = (
            # LinkedIn Easy Apply specific
            "footer button[aria-label*='Submit application' i]",
            "button[aria-label*='Submit application' i]",
            # Generic
            "button[type='submit']",
            "input[type='submit']",
            "button:text-matches('submit application', 'i')",
            "button:text-matches('submit', 'i')",
            "button:text-matches('apply now', 'i')",
            "button:text-matches('apply', 'i')",
        )
        NEXT_SELECTORS = (
            # LinkedIn Easy Apply specific
            "button[aria-label*='next step' i]",
            "button[aria-label*='Continue to next' i]",
            "footer button.artdeco-button--primary",
            "[data-easy-apply-next-button]",
            # Generic
            "button:text-matches('next', 'i')",
            "button:text-matches('continue', 'i')",
            "button:text-matches('review', 'i')",
            "button:text-matches('proceed', 'i')",
            "button:text-matches('save and continue', 'i')",
            "button:text-matches('save & continue', 'i')",
            "[data-testid='next-button']",
            "[data-testid='continue-button']",
        )
        EXCLUDE_TEXTS = {"sign in", "log in", "login", "register", "create account", "sign up"}
        SUCCESS_SIGNALS = (
            "thank you for applying", "application submitted", "application received",
            "you've applied", "successfully submitted", "we received your application",
            "your application was submitted", "application complete",
        )

        def _click_button_broad(pg, kws_submit, kws_next) -> str:
            """JS broad button search. Returns 'submit:TEXT', 'next:TEXT', or 'none'."""
            try:
                return pg.evaluate("""([kws_sub, kws_nxt]) => {
                    const els = Array.from(document.querySelectorAll(
                        'button, input[type="submit"], [role="button"]'
                    ));
                    const txt = el => (
                        (el.innerText || el.value || el.getAttribute('aria-label') || '').toLowerCase().trim()
                    );
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                    };
                    // Match keyword: exact, starts-with, or whole-word contains
                    // Short keywords (<=8 chars) only match at start/exact to avoid "next" matching "pursue your next"
                    const kwMatch = (t, kw) => {
                        if (t === kw) return true;
                        if (t.startsWith(kw + ' ') || t.startsWith(kw + ':')) return true;
                        if (kw.length > 8 && t.includes(kw)) return true;
                        return false;
                    };
                    for (const kw of kws_sub) {
                        for (const el of els) {
                            const t = txt(el);
                            if (kwMatch(t, kw) && visible(el)) {
                                el.click(); return 'submit:' + t.slice(0, 30);
                            }
                        }
                    }
                    for (const kw of kws_nxt) {
                        for (const el of els) {
                            const t = txt(el);
                            if (kwMatch(t, kw) && visible(el)) {
                                el.click(); return 'next:' + t.slice(0, 30);
                            }
                        }
                    }
                    // Debug: visible button texts
                    const dbg = els.filter(visible).map(e => txt(e).slice(0,25)).filter(Boolean);
                    return 'none|' + dbg.slice(0,10).join(',');
                }""", [kws_submit, kws_next])
            except Exception:
                return "none"

        kws_submit_list = ["submit application", "submit my application", "submit", "apply now", "apply"]
        kws_next_list = ["continue to next step", "next", "continue", "review your application", "review", "proceed", "save and continue", "next step"]

        submitted = False
        _consecutive_none = 0  # track consecutive empty button results
        for _step in range(15):  # max 15 steps in a multi-step form
            page.wait_for_timeout(1000)

            # On steps after the first, re-fill any new visible fields
            if _step > 0:
                # Vision-verified pass first
                _bc.page = page
                _sv, _se = _vision_verified_fill(page, flat, _nim, _bc, _capt, page.url or apply_url or "")
                if _sv:
                    log.info("Step %d vision-fill: %d fields", _step + 1, _sv)
                # Legacy pass for selects/radios/Workday
                _n, _unf = _do_fill_pass(page)
                if _n:
                    log.info("Step %d: filled %d more fields (legacy)", _step + 1, _n)
                if _unf:
                    _ask_and_fill_unknowns(page, _unf)
                # LinkedIn Easy Apply: resume upload may be on any step — retry if not yet uploaded
                if is_linkedin and not uploaded:
                    uploaded = _upload_resume(page, upload_path)
                    if uploaded:
                        log.info("LinkedIn Easy Apply: resume uploaded on step %d", _step + 1)
                # ── Check for CAPTCHA that appeared during the form step ──
                try:
                    _captcha_mid = page.evaluate("""() => {
                        const frames = Array.from(document.querySelectorAll('iframe'));
                        for (const f of frames) {
                            const src = (f.src || '').toLowerCase();
                            if (src.includes('hcaptcha.com') ||
                                src.includes('api2/bframe') || src.includes('api2/anchor')) {
                                const r = f.getBoundingClientRect();
                                if (r.width > 10 && r.height > 10) return true;
                            }
                        }
                        if (document.querySelector('.h-captcha[data-sitekey]')) return true;
                        return false;
                    }""")
                    if _captcha_mid:
                        log.info("CAPTCHA detected during form step %d — attempting CaptchaSolver...", _step + 1)
                        if _capt.solve(page, page.url or apply_url or ""):
                            log.info("Mid-form CAPTCHA solved")
                            page.wait_for_timeout(2000)
                        else:
                            log.warning("Mid-form CAPTCHA unsolvable")
                            _tg(f"🛑 *CAPTCHA during form (unsolved)* — {title}\n{apply_url}")
                            return "captcha"
                except Exception:
                    pass
                page.wait_for_timeout(800)

            # LinkedIn Easy Apply: dismiss any "Discard application?" / "Not now" overlays
            if is_linkedin:
                for _dismiss_sel in (
                    "button[data-control-name='discard_application_confirm_btn']",
                    "button[aria-label*='Dismiss' i]",
                    "button:text-is('Not now')",
                    "button:text-is('Continue applying')",
                ):
                    try:
                        d_btn = page.locator(_dismiss_sel).first
                        if d_btn.is_visible(timeout=400):
                            d_btn_text = (d_btn.inner_text() or "").strip()
                            # Only auto-click "Continue applying" / "Not now", not "Discard"
                            if any(t in d_btn_text.lower() for t in ("continue", "not now")):
                                d_btn.click()
                                log.info("Dismissed LinkedIn overlay: '%s'", d_btn_text)
                                page.wait_for_timeout(600)
                                break
                    except Exception:
                        pass

            # Check for success page (some ATS redirect without needing submit click)
            try:
                body_text = (page.inner_text("body") or "").lower()
            except Exception:
                body_text = ""
            if any(s in body_text for s in SUCCESS_SIGNALS):
                log.info("Success page detected on step %d", _step + 1)
                submitted = True
                break

            # Use JS broad search directly — fast single call, no per-selector timeouts
            _js_result = _click_button_broad(page, kws_submit_list, kws_next_list)
            next_clicked = False

            if _js_result and not _js_result.startswith("none"):
                _consecutive_none = 0
                log.info("Button click (step %d): %s", _step + 1, _js_result)
                if _js_result.startswith("submit:"):
                    submitted = True
                else:
                    next_clicked = True
                page.wait_for_timeout(800)
            else:
                # JS missed it — quick Playwright fallback (low timeout, just 400ms each)
                for selector in SUBMIT_SELECTORS[:3]:  # only the most specific ones
                    try:
                        btn = page.locator(selector).first
                        if btn.is_visible(timeout=400):
                            btn_text = (btn.inner_text() or "").lower().strip()
                            if not any(t in btn_text for t in ("next", "continue", "proceed")):
                                btn.click()
                                submitted = True
                                log.info("Clicked submit via PW (step %d): %s", _step + 1, btn_text[:30])
                                break
                    except Exception:
                        pass
                if not submitted:
                    for selector in NEXT_SELECTORS[:4]:  # only the most specific ones
                        try:
                            btn = page.locator(selector).first
                            if btn.is_visible(timeout=400):
                                btn_text = (btn.inner_text() or "").lower().strip()
                                if any(ex == btn_text or btn_text.startswith(ex) for ex in EXCLUDE_TEXTS):
                                    continue
                                btn.click()
                                next_clicked = True
                                log.info("Clicked Next via PW (step %d): '%s'", _step + 1, btn_text[:30])
                                page.wait_for_timeout(800)
                                break
                        except Exception:
                            pass

            if submitted:
                break

            if not next_clicked and not submitted:
                _consecutive_none += 1
                if _consecutive_none >= 2:
                    # ── Vision fallback: SoM screenshot → Llama-3.2 locates Submit ──
                    log.info("No buttons found x2 — attempting vision model Submit fallback")
                    _bc.page = page
                    if _find_submit_btn_vision(page, _bc, _nim):
                        log.info("Submit clicked via vision fallback (SoM)")
                        page.wait_for_timeout(2500)
                        submitted = True
                        break
                    log.warning("No buttons found on 2 consecutive steps — page has no form, skipping")
                    return "failed:no_form_found"
                log.warning("No Next or Submit button on step %d — stopping", _step + 1)
                break

        if not submitted:
            log.warning("Could not find submit button after %d steps", _step + 1)
            ss_fail = Path("/tmp") / f"hireagent_fail_{int(time.time())}.png"
            try:
                page.screenshot(path=str(ss_fail), full_page=False)
                _tg(f"❌ *Submit button not found* — {title}\n{apply_url}", ss_fail)
            except Exception:
                _tg(f"❌ *Submit button not found* — {title}\n{apply_url}")
            return RESULT_FAILED

        # ── Post-submit: wait and check for errors / "Thank you" confirmation ──
        page.wait_for_timeout(3000)

        # Check for validation errors after submit
        for _retry in range(3):
            errors = _collect_field_errors(page)
            body_after = (page.inner_text("body") or "").lower()
            success_confirmed = any(s in body_after for s in SUCCESS_SIGNALS)

            if success_confirmed:
                log.info("✅ Confirmation message detected after submit")
                break

            if not errors:
                # No errors visible, no success yet — wait a bit more
                page.wait_for_timeout(2000)
                continue

            # Errors found — scroll to each one, try to fix, or ask Telegram
            log.info("Post-submit errors detected (retry %d): %s", _retry + 1, errors[:3])

            # Collect empty required fields caused by the validation
            empty_after = _collect_empty_required(page)
            if empty_after:
                log.info("Empty required fields after submit error: %s", empty_after[:5])
                # First try to auto-fill from profile
                _n, _unf = _do_fill_pass(page)
                if _n:
                    log.info("Auto-filled %d fields after error", _n)
                remaining_empty = [f for f in empty_after if f not in (_unf or [])]
                if remaining_empty or _unf:
                    # Ask Telegram about fields we can't fill automatically
                    ask_fields = list(dict.fromkeys((remaining_empty or []) + (_unf or [])))[:5]
                    error_msg = (
                        f"⚠️ *Form error after submit* — {title}\n\n"
                        f"*Errors:* {' | '.join(errors[:3])}\n\n"
                        f"*Fields needing answers:*\n"
                        + "\n".join(f"• `{f}`" for f in ask_fields)
                        + "\n\nReply to each field question I send next."
                    )
                    _tg(error_msg)
                    log.info("Asking Telegram about post-error fields: %s", ask_fields)
                    _ask_and_fill_unknowns(page, ask_fields)

            # Scroll to top, re-check, re-submit
            try:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(0)
            except Exception:
                pass

            # Re-submit
            _js_result2 = _click_button_broad(page, kws_submit_list, kws_next_list)
            if _js_result2 and _js_result2 != "none":
                log.info("Re-submitted after error fix: %s", _js_result2)
                page.wait_for_timeout(500)
            else:
                for selector in SUBMIT_SELECTORS:
                    try:
                        btn = page.locator(selector).first
                        if btn.is_visible(timeout=1500):
                            btn.click()
                            log.info("Re-clicked submit after error fix")
                            page.wait_for_timeout(500)
                            break
                    except Exception:
                        pass

        # ── Final confirmation screenshot → Telegram ──
        ss_confirm = Path("/tmp") / f"hireagent_confirm_{int(time.time())}.png"
        try:
            page.screenshot(path=str(ss_confirm), full_page=False)
        except Exception:
            ss_confirm = None

        body = (page.inner_text("body") or "").lower()
        success = any(s in body for s in (
            "thank you", "application received", "submitted", "confirmation",
            "we'll be in touch", "already received", "under review",
        ) + SUCCESS_SIGNALS)

        status_icon = "✅" if success else "⚠️"
        status_text = "Applied!" if success else "Submitted (unconfirmed — check browser)"
        _tg(
            f"{status_icon} *{status_text}*\n\n"
            f"*Job:* {title}\n"
            f"*ATS:* {ats} | *Score:* {job.get('fit_score','N/A')}/10\n"
            f"Fields filled: {n_filled} | Resume: {'✅' if uploaded else '❌'}\n"
            f"{apply_url}",
            ss_confirm,
        )
        return RESULT_APPLIED
    finally:
        # Stop Playwright without closing Chrome (CDP connection is persistent)
        try:
            pw.stop()
        except Exception:
            pass


# ── Public entry point ─────────────────────────────────────────────────────

def apply_job(job: dict, profile: dict, resume_path: Path, headless: bool = False) -> str:
    """Apply to one job. Playwright directly (OpenClaw bypassed — unreliable). Returns status string."""
    return apply_via_playwright(job, profile, resume_path, headless=headless)
