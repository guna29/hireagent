"""Local Playwright-based job application filler.

Replaces Claude Code CLI with direct browser automation.
Connects to an already-launched Chrome instance via CDP.
No Claude account required.
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

from hireagent import config

logger = logging.getLogger(__name__)

_CUSTOM_ANSWERS_PATH = Path.home() / ".hireagent" / "custom_answers.json"
_custom_answers_cache: dict | None = None


def _load_custom_answers() -> dict:
    """Load ~/.hireagent/custom_answers.json (cached). Returns empty dict on error."""
    global _custom_answers_cache
    if _custom_answers_cache is not None:
        return _custom_answers_cache
    try:
        if _CUSTOM_ANSWERS_PATH.exists():
            data = json.loads(_CUSTOM_ANSWERS_PATH.read_text(encoding="utf-8"))
            _custom_answers_cache = {
                "select": data.get("select", {}),
                "text": data.get("text", {}),
                "radio": data.get("radio", {}),
            }
        else:
            _custom_answers_cache = {"select": {}, "text": {}, "radio": {}}
    except Exception as e:
        logger.debug("custom_answers.json load error: %s", e)
        _custom_answers_cache = {"select": {}, "text": {}, "radio": {}}
    return _custom_answers_cache


def _match_custom(section: str, label: str) -> str | None:
    """Check if label matches any pattern in custom_answers[section]. Returns value or None."""
    answers = _load_custom_answers().get(section, {})
    label_lower = label.lower()
    for pattern, value in answers.items():
        if pattern.lower() in label_lower:
            return value
    return None


# ---------------------------------------------------------------------------
# Profile data extraction
# ---------------------------------------------------------------------------

def _resolve_salary(job: dict, default: str = "90000") -> str:
    """Return mid-range annual salary from job description, else default.

    Handles:
    - Annual: $80,000 - $120,000 / $80k-$120k / 80000-120000 USD
    - Hourly:  $20/hr - $40/hr  → converted to annual (x 2080 hrs/year)
    - Appends nothing (caller decides formatting); returns plain integer string.
    """
    desc = (job.get("full_description") or "") + " " + (job.get("title") or "")

    # Hourly range: $20 - $40/hr  or  20-40 per hour
    hourly_pattern = re.compile(
        r'\$\s*(\d[\d,]*\.?\d*)\s*[kK]?\s*(?:to|[-–—])\s*\$?\s*(\d[\d,]*\.?\d*)\s*[kK]?'
        r'\s*(?:per hour|\/hour|\/hr|an hour|per hr)',
        re.IGNORECASE
    )
    # Annual range: $80,000 - $120,000 or $80k-$120k or 80000-120000 USD
    annual_pattern = re.compile(
        r'\$\s*(\d[\d,]*)\s*[kK]?\s*(?:to|[-–—])\s*\$?\s*(\d[\d,]*)\s*[kK]?'
        r'(?:\s*(?:USD|per year|\/yr|\/year|annually|a year))?'
        r'|(\d[\d,]+)\s*[kK]?\s*(?:to|[-–—])\s*(\d[\d,]+)\s*[kK]?\s*(?:USD|per year|\/yr|\/year|annually)',
        re.IGNORECASE
    )

    def _parse_pair(low_str: str, high_str: str, multiplier: float = 1.0) -> str | None:
        try:
            low  = float(low_str.replace(",", ""))
            high = float(high_str.replace(",", ""))
            if low < 1000:
                low *= 1000
            if high < 1000:
                high *= 1000
            mid = int(((low + high) / 2) * multiplier)
            return str(mid)
        except ValueError:
            return None

    # Check hourly first (more specific)
    m = hourly_pattern.search(desc)
    if m:
        result = _parse_pair(m.group(1), m.group(2), multiplier=2080)
        if result:
            return result

    # Then annual
    m = annual_pattern.search(desc)
    if m:
        g = m.groups()
        low_str  = (g[0] or g[2] or "").replace(",", "")
        high_str = (g[1] or g[3] or "").replace(",", "")
        if low_str and high_str:
            result = _parse_pair(low_str, high_str)
            if result:
                return result

    return default


def _build_field_data(profile: dict, job: dict | None = None) -> dict:
    """Flatten profile dict into simple key→value lookup for form filling."""
    p = profile.get("personal", {})
    wa = profile.get("work_authorization", {})
    comp = profile.get("compensation", {})
    exp = profile.get("experience", {})
    eeo = profile.get("eeo_voluntary", {})

    full_name = p.get("full_name", "")
    parts = full_name.split(None, 1)
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""

    authorized = str(wa.get("legally_authorized_to_work", "yes")).lower()
    auth_yes = authorized not in ("no", "false", "0", "")
    sponsorship = str(wa.get("require_sponsorship", "no")).lower()
    needs_sponsor = sponsorship in ("yes", "true", "1")
    avail = profile.get("availability", {})
    relocate = str(avail.get("willing_to_relocate", "yes")).lower()
    willing_to_relocate = relocate not in ("no", "false", "0", "")

    return {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "email": p.get("email", ""),
        "phone": (lambda d: d[1:] if len(d) == 11 and d.startswith("1") else d)(re.sub(r"\D", "", p.get("phone", ""))),
        "linkedin_url": p.get("linkedin_url", ""),
        "github_url": p.get("github_url", ""),
        "portfolio_url": p.get("portfolio_url") or p.get("website_url", ""),
        "address": p.get("address", ""),
        "city": p.get("city", ""),
        "state": p.get("province_state", ""),
        "zip_code": p.get("postal_code", ""),
        "country": p.get("country", "United States"),
        "salary": _resolve_salary(job or {}, default=str(comp.get("salary_expectation", "90000"))),
        "years_experience": str(exp.get("years_of_experience_total", "0")),
        "education": exp.get("education_level", ""),
        "gender": eeo.get("gender", "Decline to self-identify"),
        "race": eeo.get("race_ethnicity", "Decline to self-identify"),
        "veteran": eeo.get("veteran_status", "I am not a protected veteran"),
        "disability": eeo.get("disability_status", "I do not wish to answer"),
        "auth_yes": auth_yes,
        "needs_sponsor": needs_sponsor,
        "willing_to_relocate": willing_to_relocate,
    }


# ---------------------------------------------------------------------------
# Field label → profile key mapping
# ---------------------------------------------------------------------------

TEXT_PATTERNS: list[tuple[list[str], str]] = [
    (["first name", "first_name", "firstname", "fname", "given name", "given_name"], "first_name"),
    (["last name", "last_name", "lastname", "lname", "surname", "family name", "family_name"], "last_name"),
    (["full name", "fullname", "applicant name", "legal name", "your name", "candidate name", "your full name", "systemfield name"], "full_name"),
    (["email"], "email"),
    (["phone", "mobile", "telephone", "cell"], "phone"),
    (["linkedin"], "linkedin_url"),
    (["github"], "github_url"),
    (["portfolio", "personal website", "website"], "portfolio_url"),
    (["street address", "address line 1", "address line1", "address1"], "address"),
    (["city"], "city"),
    (["state", "province"], "state"),
    (["zip", "postal"], "zip_code"),
    (["country"], "country"),
    (["salary", "compensation", "desired salary", "expected salary", "expected compensation", "salary expectation", "pay expectation", "desired pay", "hourly rate", "hourly wage", "desired hourly", "wage", "usd", "annual salary", "base salary"], "salary"),
]


# ---------------------------------------------------------------------------
# Field detection helpers
# ---------------------------------------------------------------------------

def _get_label_text(page, element) -> str:
    """Get label text for an input via multiple strategies.

    Priority: aria-label > label[for=id] > DOM-walking > name attr > placeholder.
    Placeholder is last because many sites use generic text like "Type here...".
    """
    try:
        aria = element.get_attribute("aria-label") or ""
        if aria.strip():
            return aria.lower().strip()

        # label[for=id] is most reliable — check before placeholder
        elem_id = element.get_attribute("id") or ""
        if elem_id:
            label = page.query_selector(f"label[for='{elem_id}']")
            if label:
                return (label.inner_text() or "").lower().strip()

        name = element.get_attribute("name") or ""
        if name.strip():
            return name.lower().replace("_", " ").replace("-", " ").strip()

        placeholder = element.get_attribute("placeholder") or ""
        if placeholder.strip():
            return placeholder.lower().strip()

        # Try to find label text from surrounding DOM
        label_text = element.evaluate("""el => {
            // Walk up to find a label parent or sibling label
            let node = el;
            for (let i = 0; i < 5; i++) {
                node = node.parentElement;
                if (!node) break;
                if (node.tagName === 'LABEL') {
                    const clone = node.cloneNode(true);
                    clone.querySelectorAll('input,select,textarea,button').forEach(n => n.remove());
                    return (clone.innerText || clone.textContent || '').trim();
                }
                // Look for preceding sibling label
                const label = node.querySelector('label');
                if (label) {
                    return (label.innerText || label.textContent || '').trim();
                }
            }
            return '';
        }""")
        if label_text and label_text.strip():
            return label_text.lower().strip()
    except Exception:
        pass
    return ""


def _match_text_field(label_text: str) -> Optional[str]:
    """Match a label to a profile field key."""
    lt = label_text.lower().strip()
    for keywords, field_key in TEXT_PATTERNS:
        for kw in keywords:
            if kw in lt:
                return field_key
    # Exact-match fallbacks for short generic labels (e.g. Ashby uses just "Name")
    if lt == "name":
        return "full_name"
    if lt in ("email address", "e-mail"):
        return "email"
    return None


# ---------------------------------------------------------------------------
# Form filling
# ---------------------------------------------------------------------------

# Diagnostic messages from _fill_workday_fields — cleared + read by main loop
_wd_fill_diag: list[str] = []

_US_STATE_NAME_MAP = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas",
    "ca": "California", "co": "Colorado", "ct": "Connecticut", "de": "Delaware",
    "dc": "District of Columbia", "fl": "Florida", "ga": "Georgia", "hi": "Hawaii",
    "id": "Idaho", "il": "Illinois", "in": "Indiana", "ia": "Iowa",
    "ks": "Kansas", "ky": "Kentucky", "la": "Louisiana", "me": "Maine",
    "md": "Maryland", "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota",
    "ms": "Mississippi", "mo": "Missouri", "mt": "Montana", "ne": "Nebraska",
    "nv": "Nevada", "nh": "New Hampshire", "nj": "New Jersey", "nm": "New Mexico",
    "ny": "New York", "nc": "North Carolina", "nd": "North Dakota", "oh": "Ohio",
    "ok": "Oklahoma", "or": "Oregon", "pa": "Pennsylvania", "ri": "Rhode Island",
    "sc": "South Carolina", "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas",
    "ut": "Utah", "vt": "Vermont", "va": "Virginia", "wa": "Washington",
    "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming",
}


def _workday_select_dropdown(page, btn, target_value: "str | list[str]") -> bool:
    """Click a Workday custom dropdown button and pick the best-matching option.
    target_value may be a string or list of strings tried in order."""
    targets = [target_value] if isinstance(target_value, str) else list(target_value)
    target_lowers = [t.lower().strip() for t in targets]
    try:
        # Scroll into view first so click isn't clipped
        try:
            btn.scroll_into_view_if_needed(timeout=1000)
        except Exception:
            pass
        btn.click(force=True, timeout=3000)
        page.wait_for_timeout(1200)
        # Workday renders options in a listbox / popover
        option_locs = [
            page.locator("[data-automation-id='promptOption']"),
            page.locator("[role='option']"),
            page.locator("[role='listbox'] li"),
            page.locator("[role='listbox'] [role='option']"),
            page.locator("ul[role='listbox'] li"),
            page.locator("[aria-haspopup='listbox'] ~ * [role='option']"),
        ]
        for opt_loc in option_locs:
            try:
                count = opt_loc.count()
                if count == 0:
                    continue
                # Collect all option texts for debugging
                all_texts: list[str] = []
                for i in range(min(count, 20)):
                    try:
                        t = (opt_loc.nth(i).inner_text(timeout=500) or "").strip()
                        if t:
                            all_texts.append(t)
                    except Exception:
                        pass
                if all_texts:
                    _wd_fill_diag.append(f"[dropdown-opts] {all_texts[:8]}")
                # Try each target in priority order
                for tl in target_lowers:
                    best = None
                    for i in range(count):
                        opt = opt_loc.nth(i)
                        try:
                            txt = (opt.inner_text(timeout=500) or "").lower().strip()
                            if tl in txt or txt in tl or txt == tl:
                                best = opt
                                break
                        except Exception:
                            pass
                    if best:
                        best.click(force=True, timeout=2000)
                        page.wait_for_timeout(400)
                        return True
            except Exception:
                pass
        # Nothing matched — dismiss
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    except Exception as e:
        logger.debug("Workday dropdown click error: %s", e)
    return False


def _fill_workday_fields(page, field_data: dict) -> int:
    """Fill Workday-specific form fields using data-automation-id attributes."""
    global _wd_fill_diag
    _wd_fill_diag.clear()
    filled = 0
    # Workday uses data-automation-id on inputs
    workday_map = {
        "legalNameSection_firstName": "first_name",
        "legalNameSection_lastName": "last_name",
        "addressSection_addressLine1": "address",
        "addressSection_city": "city",
        "addressSection_postalCode": "zip_code",
        "phone-number": "phone",
        "email": "email",
        "linkedin": "linkedin_url",
        "github": "github_url",
    }
    for automation_id, field_key in workday_map.items():
        value = field_data.get(field_key, "")
        if not value:
            continue
        try:
            inp = page.query_selector(f"[data-automation-id='{automation_id}'] input, "
                                       f"input[data-automation-id='{automation_id}']")
            if inp and inp.is_visible() and not inp.is_disabled():
                try:
                    inp.fill(str(value))
                except Exception:
                    inp.click(click_count=3)
                    inp.type(str(value), delay=30)
                filled += 1
        except Exception as e:
            logger.debug("Workday field %s error: %s", automation_id, e)

    # ── Workday custom dropdown buttons (type=button showing "Select One") ──
    # These are NOT native <select> elements — they need click → listbox → option.
    state_code = (field_data.get("state") or "").lower()
    state_full = _US_STATE_NAME_MAP.get(state_code, field_data.get("state", ""))

    # Map label keywords → target value (may be list for priority fallbacks)
    _mobile_opts = ["Mobile", "Cell Phone", "Cell", "Cellular", "Mobile Phone"]
    dropdown_targets = {
        "state": state_full,
        "province": state_full,
        "phone device": _mobile_opts,
        "phone type": _mobile_opts,
        "device type": _mobile_opts,
        "country phone": ["+1", "United States", "USA", "1"],
        "country code": ["+1", "United States", "USA", "1"],
        "hear about": _load_custom_answers().get("select", {}).get("where did you hear", "LinkedIn"),
        "how did you hear": _load_custom_answers().get("select", {}).get("how did you hear", "LinkedIn"),
    }

    try:
        # Find all visible buttons that show a placeholder-style text
        placeholder_btns = page.query_selector_all("button[type='button'], [role='button']")
        positional_unfilled = []  # (y_pos, btn) for buttons whose label we couldn't detect
        for btn in placeholder_btns:
            try:
                if not btn.is_visible():
                    continue
                btn_text = (btn.inner_text() or "").strip().lower()
                # Match "select one", "select...", etc. — allow trailing icon chars
                if not ("select" in btn_text and len(btn_text) < 25):
                    continue
                # Find label for this button by walking up the DOM (up to 10 levels)
                label_text = page.evaluate("""(btn) => {
                    // Try aria-labelledby first
                    const lby = btn.getAttribute('aria-labelledby');
                    if (lby) {
                        const el = document.getElementById(lby);
                        if (el) return (el.textContent || '').trim().toLowerCase();
                    }
                    // Try aria-label on button itself
                    const al = btn.getAttribute('aria-label');
                    if (al) return al.toLowerCase();
                    // Walk up DOM
                    let el = btn;
                    for (let i = 0; i < 10; i++) {
                        el = el.parentElement;
                        if (!el) break;
                        const lbl = el.querySelector('label, [data-automation-id*="label"], [class*="label"], [class*="Label"]');
                        if (lbl && lbl !== btn) return (lbl.textContent || '').trim().toLowerCase();
                        const aria = el.getAttribute('aria-label');
                        if (aria) return aria.toLowerCase();
                        const alby = el.getAttribute('aria-labelledby');
                        if (alby) {
                            const ref = document.getElementById(alby);
                            if (ref) return (ref.textContent || '').trim().toLowerCase();
                        }
                    }
                    return '';
                }""", btn)
                matched = False
                if label_text:
                    for kw, target_val in dropdown_targets.items():
                        if kw in label_text and target_val:
                            if _workday_select_dropdown(page, btn, target_val):
                                filled += 1
                                _wd_fill_diag.append(f"[wd-dropdown] label='{label_text[:40]}' → '{target_val}' OK")
                            else:
                                _wd_fill_diag.append(f"[wd-dropdown] label='{label_text[:40]}' → '{target_val}' FAILED")
                            matched = True
                            break
                if not matched:
                    try:
                        bb = btn.bounding_box()
                        y_pos = int(bb["y"]) if bb else 9999
                    except Exception:
                        y_pos = 9999
                    positional_unfilled.append((y_pos, btn, label_text))
                    _wd_fill_diag.append(f"[wd-dropdown] no-label btn y={y_pos} text='{btn_text[:20]}' label='{label_text[:40]}'")
            except Exception as e:
                logger.debug("Workday dropdown btn error: %s", e)

        # Positional fallback: first unfilled "Select One" → state, second → phone type
        positional_unfilled.sort(key=lambda x: x[0])
        fallback_order = [
            ("state", state_full),
            ("phone type", _mobile_opts),
        ]
        for i, (y_pos, btn, lbl) in enumerate(positional_unfilled):
            if i < len(fallback_order):
                kw, target_val = fallback_order[i]
                if target_val:
                    if _workday_select_dropdown(page, btn, target_val):
                        filled += 1
                        _wd_fill_diag.append(f"[wd-dropdown-fallback] pos={i} '{kw}' → '{target_val}' OK")
                    else:
                        _wd_fill_diag.append(f"[wd-dropdown-fallback] pos={i} '{kw}' → '{target_val}' FAILED")
    except Exception as e:
        logger.debug("Workday dropdown scan error: %s", e)

    return filled


def _fill_text_inputs(page, field_data: dict) -> int:
    """Fill visible text/email/tel/url inputs. Returns count filled."""
    filled = 0
    # Also try Workday-specific fields
    filled += _fill_workday_fields(page, field_data)

    selectors = (
        "input[type='text'], input[type='email'], input[type='tel'], "
        "input[type='url'], input:not([type]), input[type='number']"
    )
    inputs = page.query_selector_all(selectors)
    for inp in inputs:
        try:
            if not inp.is_visible() or inp.is_disabled():
                continue
            label = _get_label_text(page, inp)
            if not label:
                continue
            # Check custom_answers.json first, then profile-based matching
            custom_val = _match_custom("text", label)
            field_key = _match_text_field(label)
            value = custom_val or (field_data.get(field_key) if field_key else None)
            if not value:
                continue
            # Check current value — skip only if it already matches what we'd fill.
            # Do NOT skip if ATS pre-filled the wrong value (bad resume parse).
            current = (inp.input_value() or "").strip()
            if current and current.lower() == str(value).lower():
                continue  # Already correct
            # Override pre-filled or empty fields with profile data
            try:
                inp.fill(str(value))
            except Exception:
                # fill() may fail on some inputs; fall back to click+type
                inp.click(click_count=3)
                inp.type(str(value), delay=30)
            filled += 1
            logger.debug("[fill] %s → %s (label='%s', was='%s')", field_key, str(value)[:30], label[:40], current[:20])
        except Exception as e:
            logger.debug("Text fill error: %s", e)
    return filled


def _fill_selects(page, field_data: dict) -> int:
    """Fill visible select dropdowns. Returns count filled."""
    filled = 0
    selects = page.query_selector_all("select")
    for sel in selects:
        try:
            if not sel.is_visible() or sel.is_disabled():
                continue
            label = _get_label_text(page, sel)
            lt = label.lower()

            target_value = _match_custom("select", label)
            if target_value is None and any(kw in lt for kw in ["authorized to work", "legally authorized", "work auth", "work authorization"]):
                target_value = "Yes" if field_data["auth_yes"] else "No"
            elif any(kw in lt for kw in ["sponsorship", "visa sponsor", "require sponsor", "need sponsor"]):
                target_value = "Yes" if field_data["needs_sponsor"] else "No"
            elif "country" in lt:
                target_value = "United States"
            elif ("state" in lt or "province" in lt) and field_data.get("state"):
                target_value = field_data["state"]
            elif "gender" in lt:
                target_value = field_data.get("gender", "Decline to self-identify")
            elif "race" in lt or "ethnicity" in lt:
                target_value = field_data.get("race", "Decline to self-identify")
            elif "veteran" in lt:
                target_value = field_data.get("veteran", "I am not a protected veteran")
            elif "disability" in lt:
                target_value = field_data.get("disability", "I do not wish to answer")

            if target_value and _try_select_value(sel, target_value):
                filled += 1
                logger.debug("[select] label='%s' → '%s'", label[:40], target_value)
        except Exception as e:
            logger.debug("Select fill error: %s", e)
    return filled


def _try_select_value(select_el, target_value: str) -> bool:
    """Select the option best matching target_value (fuzzy). Returns True if found."""
    target = target_value.lower().strip()
    options = select_el.query_selector_all("option")
    best_opt_value = None
    for opt in options:
        opt_text = (opt.inner_text() or "").lower().strip()
        opt_val = (opt.get_attribute("value") or "").lower().strip()
        # Skip placeholder options
        if opt_val in ("", "placeholder", "select", "select one", "--select--"):
            continue
        if opt_text in ("select...", "select one", "-- select --", "", "--"):
            continue
        if target in opt_text or opt_text in target:
            best_opt_value = opt.get_attribute("value")
            break
        if target in opt_val or opt_val in target:
            best_opt_value = opt.get_attribute("value")
    if best_opt_value is not None:
        try:
            select_el.select_option(value=best_opt_value)
            return True
        except Exception:
            pass
    return False


def _fill_radio_checkboxes(page, field_data: dict) -> int:
    """Handle radio button groups for yes/no questions. Returns groups handled."""
    handled = 0
    # Group radios by name attribute
    radio_groups: dict[str, list] = {}
    for radio in page.query_selector_all("input[type='radio']"):
        name = radio.get_attribute("name") or ""
        if name:
            radio_groups.setdefault(name, []).append(radio)

    for name, group in radio_groups.items():
        try:
            lt = name.lower().replace("_", " ").replace("-", " ")

            desired_yes: Optional[bool] = None
            custom_radio = _match_custom("radio", lt)
            if custom_radio is not None:
                desired_yes = custom_radio.lower() not in ("no", "false", "0", "n")
            elif any(kw in lt for kw in ["authorized", "legal", "work auth"]):
                desired_yes = field_data["auth_yes"]
            elif any(kw in lt for kw in ["sponsor", "visa"]):
                # "Do you need sponsorship?" → answer No if you don't need it
                desired_yes = field_data["needs_sponsor"]
            elif any(kw in lt for kw in ["relocat", "willing to move", "open to relocation"]):
                desired_yes = field_data["willing_to_relocate"]
            elif any(kw in lt for kw in ["18", "age", "adult", "old enough"]):
                desired_yes = True
            elif any(kw in lt for kw in ["background check", "drug test", "drug screen"]):
                desired_yes = True
            elif any(kw in lt for kw in ["felony", "criminal"]):
                desired_yes = False
            elif any(kw in lt for kw in ["previously worked", "worked here before"]):
                desired_yes = False

            if desired_yes is None:
                continue

            target = "yes" if desired_yes else "no"
            for radio in group:
                val = (radio.get_attribute("value") or "").lower()
                aria = (radio.get_attribute("aria-label") or "").lower()
                rid = radio.get_attribute("id") or ""
                label_text = ""
                if rid:
                    lbl = page.query_selector(f"label[for='{rid}']")
                    if lbl:
                        label_text = (lbl.inner_text() or "").lower()
                combined = f"{val} {aria} {label_text}"
                if target in combined or combined.strip() == target:
                    if radio.is_visible() and not radio.is_disabled():
                        radio.click()
                        handled += 1
                        break
        except Exception as e:
            logger.debug("Radio fill error for '%s': %s", name, e)
    return handled


def _upload_resume(page, resume_pdf: str) -> bool:
    """Upload resume PDF to any file input found on the page.

    Handles standard <input type='file'>, drag-drop areas, and Indeed Easy Apply's
    'Add a resume' / 'Upload resume' / 'Change resume' button patterns.
    """
    pdf_path = Path(resume_pdf)
    if not pdf_path.exists():
        logger.warning("Resume PDF not found at: %s", pdf_path)
        return False

    # Try direct hidden/visible file inputs first
    for inp in page.query_selector_all("input[type='file']"):
        try:
            accept = (inp.get_attribute("accept") or "").lower()
            # Only skip if accept explicitly excludes PDFs
            if accept and "pdf" not in accept and "*" not in accept and "application" not in accept:
                continue
            inp.set_input_files(str(pdf_path))
            logger.info("Uploaded resume via file input: %s", pdf_path.name)
            return True
        except Exception as e:
            logger.debug("File input upload error: %s", e)

    # Try clicking upload/resume buttons that trigger a file chooser
    upload_kws = [
        "upload resume", "upload cv", "upload a resume", "add resume", "add a resume",
        "attach resume", "change resume", "replace resume", "upload your resume",
        "choose file", "select file", "add file", "attach file",
        "upload", "attach", "resume", "cv",
    ]
    for btn in page.query_selector_all("button, [role='button'], label, a"):
        try:
            if not btn.is_visible():
                continue
            text = (btn.inner_text() or btn.get_attribute("aria-label") or "").lower().strip()
            if not any(kw in text for kw in upload_kws):
                continue
            with page.expect_file_chooser(timeout=5000) as fc_info:
                btn.click()
            fc_info.value.set_files(str(pdf_path))
            logger.info("Uploaded resume via file chooser ('%s'): %s", text[:40], pdf_path.name)
            return True
        except Exception:
            pass

    # Last resort: try JS to find any hidden file input and set its files
    # (some drag-drop zones have a visually hidden <input type='file'>)
    try:
        result = page.evaluate(f"""(pdfPath) => {{
            const inputs = Array.from(document.querySelectorAll("input[type='file']"));
            for (const inp of inputs) {{
                const accept = (inp.getAttribute('accept') || '').toLowerCase();
                if (accept && !accept.includes('pdf') && !accept.includes('*') && !accept.includes('application')) continue;
                // Temporarily make visible for set_input_files
                inp.style.display = 'block';
                inp.style.opacity = '1';
                inp.style.position = 'fixed';
                inp.style.top = '0';
                inp.style.left = '0';
                inp.style.zIndex = '99999';
                return inp.id || inp.name || 'found';
            }}
            return 'not_found';
        }}""", str(pdf_path))
        if result != "not_found":
            # Now the input is visible — try again
            for inp in page.query_selector_all("input[type='file']"):
                try:
                    inp.set_input_files(str(pdf_path))
                    logger.info("Uploaded resume via JS-unhidden file input: %s", pdf_path.name)
                    return True
                except Exception:
                    pass
    except Exception as e:
        logger.debug("JS file input reveal error: %s", e)

    logger.debug("No file upload input found on this page")
    return False


def _detect_captcha(page) -> bool:
    """Detect a visible CAPTCHA challenge on the current page.

    Ignores reCAPTCHA v3 (invisible/background) — only flags challenges that
    actually block the user, like v2 checkbox, hCaptcha, or Cloudflare Turnstile.
    """
    try:
        # Visible reCAPTCHA v2: challenge iframe or .g-recaptcha div
        if page.query_selector("iframe[src*='recaptcha/api2/bframe']"):
            return True
        if page.query_selector(".g-recaptcha[data-sitekey]"):
            return True
        # hCaptcha
        if page.query_selector("iframe[src*='hcaptcha.com']"):
            return True
        # Cloudflare Turnstile
        if page.query_selector(".cf-turnstile, iframe[src*='challenges.cloudflare.com']"):
            return True
        return False
    except Exception:
        return False


def _wait_for_page_ready(page, timeout_ms: int = 8000) -> None:
    """Wait for a SPA to finish rendering after navigation."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def _handle_workday_login(page, log_fn) -> bool:
    """Detect and handle Workday sign-in / create-account wall.

    Returns True if we successfully got past the login screen.
    Credentials come from ~/.hireagent/.env: WORKDAY_EMAIL / WORKDAY_PASSWORD.
    """
    import os
    email = os.environ.get("WORKDAY_EMAIL", "")
    password = os.environ.get("WORKDAY_PASSWORD", "")
    if not email or not password:
        return False

    try:
        url = page.url.lower()
        # Workday sign-in pages contain these markers
        is_workday_auth = (
            "myworkdayjobs.com" in url or "wd1.myworkdayjobs" in url or
            "wd3.myworkdayjobs" in url or "wd5.myworkdayjobs" in url
        )
        if not is_workday_auth:
            return False
        # Require a stronger signal: URL path indicates auth/apply flow,
        # OR there's an actual email input (or sign-in page title)
        url_is_auth = any(s in url for s in [
            "/login", "/signin", "/sign-in", "/createaccount", "/create-account",
            "/apply/", "/apply", "apply?", "workdayaccounts"
        ])
        # Also check page title for "Sign In"
        title_is_auth = False
        try:
            title_is_auth = "sign in" in page.title().lower()
        except Exception:
            pass
        # Use Locator to pierce Shadow DOM when checking for email input
        has_email_input = False
        try:
            email_loc = page.locator(
                "[data-automation-id='email'], input[type='email'], "
                "input[name='email'], [data-automation-id='username'], "
                "input[autocomplete='email'], input[autocomplete='username']"
            )
            has_email_input = email_loc.first.is_visible()
        except Exception:
            pass
        if not (url_is_auth or title_is_auth or has_email_input):
            return False
    except Exception:
        return False

    log_fn("Workday sign-in page detected — attempting login")

    # ── Dismiss cookie banner first (blocks form interaction) ────────────────
    try:
        cookie_loc = page.locator(
            "button:has-text('Accept Cookies'), button:has-text('Accept All'), "
            "button:has-text('Accept all'), button:has-text('I Accept'), "
            "button:has-text('Accept'), button[id*='accept'], button[id*='cookie']"
        )
        if cookie_loc.first.is_visible():
            cookie_loc.first.click()
            page.wait_for_timeout(1500)
            log_fn("Dismissed cookie consent banner")
    except Exception:
        pass

    # ── Click "Apply Manually" if present (Workday intermediate screen) ───────
    try:
        manual_loc = page.locator(
            "button:has-text('Apply Manually'), a:has-text('Apply Manually'), "
            "[data-automation-id='applyManuallyButton']"
        )
        if manual_loc.first.is_visible():
            manual_loc.first.click()
            page.wait_for_timeout(3500)  # Wait for sign-in options to render
            log_fn("Clicked 'Apply Manually'")
    except Exception:
        pass

    def _loc_visible(locator) -> bool:
        """Check if a Playwright Locator has ≥1 visible element."""
        try:
            return locator.first.is_visible()
        except Exception:
            return False

    def _loc_fill(locator, value):
        """Fill a Playwright Locator, falling back to click+type."""
        try:
            locator.first.fill(str(value))
        except Exception:
            try:
                locator.first.click(click_count=3)
                locator.first.type(str(value), delay=40)
            except Exception:
                pass

    # ── Step 1: Click Workday "Sign In" section header (avoid Apple/Google SSO)
    # Use data-automation-id first (Workday-specific), then fall back to
    # text matching but skip SSO provider buttons (Apple, Google, Microsoft).
    signin_clicked = False
    for sel in [
        "[data-automation-id='signInLink']",
        "[data-automation-id='existing-account-link']",
        "[data-automation-id='signInWithWorkdayButton']",
        "[data-automation-id='signInWithEmail']",
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(1500)
                log_fn(f"Clicked sign-in link ({sel})")
                signin_clicked = True
                break
        except Exception:
            pass

    if not signin_clicked:
        # Text-based: find "Sign In" buttons/links that are NOT SSO providers
        # Priority order: email-specific first, then generic Sign In (below nav)
        sign_in_texts_priority = [
            "sign in with email", "sign in with username", "continue with email",
            "use email", "sign in with workday", "sign in"
        ]
        # Retry up to 5 times (some SPAs render the email sign-in option async)
        for _si_attempt in range(5):
            try:
                btns = page.query_selector_all("button, a, [role='button']")
                best_btn = None
                best_priority = 999
                for btn in btns:
                    try:
                        if not btn.is_visible():
                            continue
                        txt = (btn.inner_text() or "").strip().lower()
                        aria = (btn.get_attribute("aria-label") or "").lower()
                        combined = txt + " " + aria
                        # Skip SSO provider buttons
                        if any(sso in combined for sso in ["apple", "google", "microsoft", "linkedin", "facebook", "seek"]):
                            continue
                        for i, sig_txt in enumerate(sign_in_texts_priority):
                            if sig_txt in combined or combined == sig_txt:
                                box = btn.bounding_box()
                                # Skip nav bar (top ~100px) for generic "sign in"
                                if sig_txt == "sign in" and box and box.get("y", 0) <= 100:
                                    continue
                                if i < best_priority:
                                    best_priority = i
                                    best_btn = btn
                                break
                    except Exception:
                        pass
                if best_btn:
                    box = best_btn.bounding_box() or {}
                    txt = (best_btn.inner_text() or "").strip().lower()
                    best_btn.click()
                    page.wait_for_timeout(1500)
                    log_fn(f"Clicked sign-in button: '{txt}' (y={box.get('y',0):.0f})")
                    signin_clicked = True
                    break
                # If not found yet, wait and retry
                page.wait_for_timeout(1000)
            except Exception:
                page.wait_for_timeout(1000)

    # ── Step 2: Find email field — use JS shadow DOM traversal as fallback ──
    # First try standard Playwright Locator (pierces shadow DOM in many configs)
    email_loc = page.locator(
        "[data-automation-id='email'], input[type='email'], "
        "input[name='email'], [data-automation-id='username'], "
        "input[autocomplete='email'], input[autocomplete='username']"
    )
    email_filled = False
    for attempt in range(8):
        try:
            if _loc_visible(email_loc):
                _loc_fill(email_loc, email)
                email_filled = True
                log_fn(f"Filled Workday email via Locator (attempt {attempt})")
                break
        except Exception as e:
            log_fn(f"Workday email fill error (attempt {attempt}): {e}")

        # Fallback: JS to find input in shadow DOM and fill it
        if not email_filled:
            try:
                filled = page.evaluate(f"""(emailValue) => {{
                    function findAndFill(root) {{
                        const inputs = root.querySelectorAll(
                            "input[type='email'], input[autocomplete='email'], " +
                            "input[autocomplete='username'], input[name='email'], " +
                            "input[name='username'], [data-automation-id='email']"
                        );
                        for (const inp of inputs) {{
                            if (inp.offsetParent !== null || inp.getBoundingClientRect().height > 0) {{
                                inp.focus();
                                inp.value = emailValue;
                                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                                return true;
                            }}
                        }}
                        const allEls = root.querySelectorAll('*');
                        for (const el of allEls) {{
                            if (el.shadowRoot) {{
                                if (findAndFill(el.shadowRoot)) return true;
                            }}
                        }}
                        return false;
                    }}
                    return findAndFill(document);
                }}""", email)
                if filled:
                    email_filled = True
                    log_fn(f"Filled Workday email via JS shadow DOM traversal (attempt {attempt})")
                    break
            except Exception as e:
                log_fn(f"Workday JS email fill error (attempt {attempt}): {e}")

        page.wait_for_timeout(1000)

    if not email_filled:
        log_fn(f"Workday: email field not found via Locator/JS. URL={page.url[:80]}")
        return False

    # ── Step 3: Click Continue / Next ─────────────────────────────────────────
    page.wait_for_timeout(500)
    continue_loc = page.locator(
        "[data-automation-id='continue-button'], [data-automation-id='next-button'], "
        "button:has-text('Continue'), input[value='Continue'], "
        "button:has-text('Next'), input[value='Next']"
    )
    try:
        if _loc_visible(continue_loc):
            continue_loc.first.click()
            log_fn("Clicked Continue/Next")
            page.wait_for_timeout(2500)
    except Exception as e:
        log_fn(f"Continue button click error: {e}")

    # After Continue, Workday may show "Sign In" link for existing accounts
    for text in ["Sign In", "Already have an account"]:
        try:
            signin_lnk = page.locator(
                f"[data-automation-id='signInLink'], "
                f"[data-automation-id='existing-account-link'], "
                f"a:has-text('{text}')"
            )
            if _loc_visible(signin_lnk):
                signin_lnk.first.click()
                page.wait_for_timeout(2000)
                log_fn(f"Clicked '{text}' link after Continue")
                break
        except Exception:
            pass

    # ── Step 4: Wait for password field (up to 8s) ───────────────────────────
    pw_loc = page.locator(
        "[data-automation-id='password'], input[type='password'], "
        "input[name='password'], input[autocomplete='current-password']"
    )
    pw_found = False
    for _ in range(8):
        try:
            if _loc_visible(pw_loc):
                pw_found = True
                break
        except Exception:
            pass
        # Also check via JS shadow DOM
        try:
            found_js = page.evaluate("""() => {
                function findPw(root) {
                    const inputs = root.querySelectorAll(
                        "input[type='password'], [data-automation-id='password']"
                    );
                    for (const inp of inputs) {
                        if (inp.offsetParent !== null || inp.getBoundingClientRect().height > 0)
                            return true;
                    }
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot && findPw(el.shadowRoot)) return true;
                    }
                    return false;
                }
                return findPw(document);
            }""")
            if found_js:
                pw_found = True
                break
        except Exception:
            pass
        page.wait_for_timeout(1000)

    if pw_found:
        # ── Step 5: Fill password via Locator or JS ──────────────────────────
        pw_filled = False
        try:
            if _loc_visible(pw_loc):
                _loc_fill(pw_loc, password)
                pw_filled = True
                log_fn("Filled Workday password via Locator")
        except Exception as e:
            log_fn(f"Workday password Locator fill error: {e}")

        if not pw_filled:
            try:
                ok = page.evaluate(f"""(pw) => {{
                    function fillPw(root) {{
                        const inputs = root.querySelectorAll(
                            "input[type='password'], [data-automation-id='password']"
                        );
                        for (const inp of inputs) {{
                            if (inp.offsetParent !== null || inp.getBoundingClientRect().height > 0) {{
                                inp.focus();
                                inp.value = pw;
                                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                                return true;
                            }}
                        }}
                        for (const el of root.querySelectorAll('*')) {{
                            if (el.shadowRoot && fillPw(el.shadowRoot)) return true;
                        }}
                        return false;
                    }}
                    return fillPw(document);
                }}""", password)
                if ok:
                    log_fn("Filled Workday password via JS")
            except Exception as e:
                log_fn(f"Workday password JS fill error: {e}")

        # Click Sign In — try locator first, then JS to bypass consent overlay
        sign_loc = page.locator(
            "[data-automation-id='sign-in-button']:not([aria-hidden='true']), "
            "[data-automation-id='signInButton']:not([aria-hidden='true']), "
            "button:has-text('Sign In'):not([aria-hidden='true']), "
            "input[value='Sign In']"
        )
        _signin_done = False
        try:
            if _loc_visible(sign_loc):
                sign_loc.first.click(timeout=5000)
                page.wait_for_timeout(4000)
                log_fn("Workday sign-in submitted — waiting for redirect")
                _signin_done = True
        except Exception as e:
            log_fn(f"Workday sign-in button error: {e}")

        if not _signin_done:
            # JS click bypasses consent modal overlay that intercepts pointer events
            try:
                js_result = page.evaluate("""() => {
                    const btn = document.querySelector(
                        "[data-automation-id='signInSubmitButton'], " +
                        "[data-automation-id='sign-in-button'], " +
                        "button[type='submit']:not([data-automation-id='utilityButtonSignIn'])"
                    );
                    if (btn) { btn.click(); return 'clicked:' + (btn.textContent||'').trim().slice(0,30); }
                    const modal = document.querySelector('[data-behavior-click-outside-close]');
                    if (modal) {
                        const btns = modal.querySelectorAll('button, [role=button]');
                        for (const b of btns) {
                            const t = (b.textContent||'').toLowerCase().trim();
                            if (t.includes('sign') || t.includes('agree') || t.includes('accept')) {
                                b.click(); return 'modal:' + t.slice(0,30);
                            }
                        }
                    }
                    return 'not_found';
                }""")
                page.wait_for_timeout(4000)
                log_fn(f"Workday sign-in JS click: {js_result}")
                _signin_done = True
            except Exception as e:
                log_fn(f"Workday sign-in JS error: {e}")

        if _signin_done:
            return True

        log_fn("Workday: could not find Sign In button after password")
        return False

    # ── Fallback: create account flow ─────────────────────────────────────────
    log_fn("Workday: password not found — trying Create Account flow")
    try:
        ca_loc = page.locator(
            "[data-automation-id='createAccountLink'], "
            "a:has-text('Create Account'), button:has-text('Create Account')"
        )
        if _loc_visible(ca_loc):
            ca_loc.first.click()
            page.wait_for_timeout(2000)
            log_fn("Clicked Create Account")

            # Fill email
            email_loc2 = page.locator(
                "[data-automation-id='email'], input[type='email']"
            )
            if _loc_visible(email_loc2):
                _loc_fill(email_loc2, email)

            # Fill password fields (password + confirm password)
            pw_fields_loc = page.locator(
                "[data-automation-id='password'], input[type='password']"
            )
            try:
                count = pw_fields_loc.count()
                for i in range(count):
                    pw_el = pw_fields_loc.nth(i)
                    if pw_el.is_visible():
                        pw_el.fill(password)
            except Exception:
                pass

            # Submit
            submit_loc2 = page.locator(
                "[data-automation-id='createAccountButton'], "
                "button:has-text('Create Account'), button[type='submit']"
            )
            if _loc_visible(submit_loc2):
                submit_loc2.first.click()
                page.wait_for_timeout(3000)
                log_fn("Workday create account submitted")
                return True
    except Exception as e:
        log_fn(f"Workday create account error: {e}")

    return False


def _click_apply_cta(page) -> bool:
    """Click the primary 'Apply' CTA button on job description pages.

    Tries ATS-specific selectors first, then falls back to text matching.
    Returns True if a button was found and clicked.
    """
    # ATS-specific apply button selectors (checked first)
    ats_selectors = [
        # Indeed Easy Apply
        "[data-testid='IndeedApplyButton']",
        "button[data-indeed-apply-joburl]",
        ".ia-IndeedApplyButton",
        ".indeed-apply-button",
        # Indeed external apply (company site)
        "[data-testid='job-apply-button']",
        "[data-indeed-apply]",
        # Workday
        "[data-automation-id='applyButton']",
        "[data-automation-id='apply-button']",
        "[data-automation-id='Apply']",
        # Greenhouse
        "#apply_button",
        ".application--cta a",
        # Lever
        ".postings-btn-wrapper a",
        ".template-btn-submit",
        # Ashby
        "[data-testid='apply-button']",
        # SmartRecruiters
        ".js-apply-btn",
        # Rippling
        "[data-testid='apply-now-button']",
        # iCIMS
        ".iCIMS_Anchor[title*='Apply']",
    ]
    for sel in ats_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                logger.info("Clicked ATS-specific Apply CTA: %s", sel)
                return True
        except Exception:
            pass

    # Fallback: text-based matching
    apply_kws = [
        "apply now", "apply for this job", "apply to this job", "apply for job",
        "start application", "begin application", "easy apply", "quick apply",
        "easily apply", "apply on company site", "apply on employer site",
        "apply on indeed", "apply with indeed", "apply",
    ]
    candidates = page.query_selector_all(
        "button, a, [role='button'], input[type='button'], input[type='submit']"
    )
    best = None
    best_score = -1
    for el in candidates:
        try:
            if not el.is_visible():
                continue
            text = (el.inner_text() or el.get_attribute("value") or "").lower().strip()
            for i, kw in enumerate(apply_kws):
                if text == kw or text.startswith(kw):
                    score = len(apply_kws) - i
                    if score > best_score:
                        best_score = score
                        best = el
                    break
        except Exception:
            pass
    if best is not None:
        try:
            best.click()
            logger.info("Clicked Apply CTA (text match)")
            return True
        except Exception as e:
            logger.debug("CTA click error: %s", e)
    return False


def _has_form_elements(page) -> bool:
    """Check if the page has an active application form (not just nav/search inputs).

    Returns True only if there are meaningful application form fields — not just
    a single search box or header input that exists on every job description page.
    """
    try:
        # Exclude search/button/hidden inputs; type='search' is always a search box
        inputs = page.query_selector_all(
            "input[type='text'], input[type='email'], input[type='tel'], "
            "input[type='url'], input[type='number'], input[type='password'], "
            "input:not([type]), select, textarea"
        )
        visible = []
        for el in inputs:
            try:
                if not el.is_visible():
                    continue
                # Skip inputs inside nav / header / search containers
                in_chrome = el.evaluate("""el => {
                    let node = el;
                    for (let i = 0; i < 8; i++) {
                        node = node.parentElement;
                        if (!node) break;
                        const tag = (node.tagName || '').toLowerCase();
                        const role = (node.getAttribute('role') || '').toLowerCase();
                        const cls = (node.className || '').toLowerCase();
                        if (['header', 'nav'].includes(tag) ||
                            role === 'navigation' || role === 'search' ||
                            cls.includes('header') || cls.includes('navbar') ||
                            cls.includes('search-bar') || cls.includes('searchbar')) {
                            return true;
                        }
                    }
                    return false;
                }""")
                if not in_chrome:
                    visible.append(el)
            except Exception:
                visible.append(el)
        # Require at least 2 meaningful inputs to distinguish an application form
        # from a single search box on a job description page.
        return len(visible) >= 2
    except Exception:
        return False


def _detect_success(page) -> bool:
    """Detect successful application submission."""
    try:
        url = page.url.lower()
        content = page.content().lower()
    except Exception:
        return False

    url_signals = ["thank", "confirm", "success", "submitted", "complete", "received", "done"]
    content_signals = [
        "thank you for applying",
        "application submitted",
        "application received",
        "application complete",
        "successfully submitted",
        "you have applied",
        "your application has been",
        "we've received your application",
        "we have received your application",
        "application confirmation",
        "your application was submitted",
    ]

    for s in url_signals:
        if s in url:
            return True
    for s in content_signals:
        if s in content:
            return True
    return False


def _find_submit_or_next(page) -> tuple[str, object]:
    """Find the best button to click. Returns ('submit'|'next'|'none', element).

    Checks ATS-specific selectors first, then falls back to text matching.
    """
    # ATS-specific submit selectors
    ats_submit_selectors = [
        "[data-automation-id='bottom-navigation-next-button']",  # Workday Next
        "[data-automation-id='wd-CommandButton_uic_submitAction']",  # Workday Submit
    ]
    ats_next_selectors = [
        "[data-automation-id='bottom-navigation-next-button']",  # Workday
        "[data-automation-id='nextButton']",
        ".ia-continueButton",  # iCIMS
        ".js-continue",  # SmartRecruiters
    ]

    # Check Workday submit/review button specifically
    # Use Locator (not query_selector) so it pierces shadow DOM
    for sel in [
        "[data-automation-id='bottom-navigation-next-button']",
        "[data-automation-id='bottom-navigation-finish-button']",
        "[data-automation-id='bottom-navigation-review-button']",
        "[data-automation-id='wd-CommandButton_uic_submitAction']",
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=500):
                btn_text = (loc.inner_text(timeout=1000) or "").lower()
                if any(kw in btn_text for kw in ["submit", "review", "finish", "complete"]):
                    return "submit", loc
                else:
                    return "next", loc
        except Exception:
            pass

    submit_kws = ["submit application", "submit", "apply now", "apply", "send application",
                  "complete application", "finish", "done", "review"]
    next_kws = ["next", "continue", "proceed", "save and continue", "save & continue",
                "save and next", "next step"]
    # Nav/auth buttons that should never be form submit actions
    exclude_as_submit = ["sign in", "signin", "log in", "login", "register", "create account",
                         "sign up", "forget password", "forgot password"]

    all_buttons = page.query_selector_all(
        "button[type='submit'], input[type='submit'], button, [role='button'], a[role='button']"
    )

    submit_candidates = []
    next_candidates = []

    for btn in all_buttons:
        try:
            if not btn.is_visible():
                continue
            disabled = btn.get_attribute("disabled")
            aria_disabled = btn.get_attribute("aria-disabled")
            if disabled is not None or aria_disabled == "true":
                continue
            text = (btn.inner_text() or btn.get_attribute("value") or "").lower().strip()
            btn_type = (btn.get_attribute("type") or "").lower()
            # Text always takes priority over button type:
            # "Next"/"Continue" buttons are "next" even if type='submit'
            is_next_text = any(kw in text for kw in next_kws)
            is_submit_text = any(kw in text for kw in submit_kws) and not is_next_text
            is_excluded = any(ex == text or text.startswith(ex) for ex in exclude_as_submit)
            # Positional check: buttons in top nav (y <= 120px) are navigation, not form buttons.
            # A "sign in" button below 120px is likely a form-level auth step button.
            if is_excluded:
                try:
                    box = btn.bounding_box()
                    if box and box.get("y", 0) > 120:
                        is_excluded = False  # form-level button, not nav
                except Exception:
                    pass
            if is_next_text:
                next_candidates.append((text, btn))
            elif is_excluded:
                pass  # Skip nav/auth buttons in top bar
            elif is_submit_text:
                submit_candidates.append((text, btn))
            elif btn_type == "submit":
                submit_candidates.insert(0, (text, btn))
        except Exception:
            pass

    if submit_candidates:
        logger.debug("Submit button found: %s", submit_candidates[0][0])
        return "submit", submit_candidates[0][1]
    if next_candidates:
        logger.debug("Next button found: %s", next_candidates[0][0])
        return "next", next_candidates[0][1]
    # Log all visible buttons for diagnosis
    try:
        all_vis = page.query_selector_all("button, [role='button'], input[type='submit']")
        vis_info = []
        for b in all_vis[:20]:
            try:
                if b.is_visible():
                    t = (b.inner_text() or b.get_attribute("value") or "").strip()[:30]
                    bt = b.get_attribute("type") or ""
                    box = b.bounding_box() or {}
                    vis_info.append(f"'{t}' type={bt} y={int(box.get('y', -1))}")
            except Exception:
                pass
        if vis_info:
            logger.debug("No btn found. Visible buttons: %s", " | ".join(vis_info))
    except Exception:
        pass
    return "none", None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_playwright_apply(
    job: dict,
    port: int,
    worker_id: int = 0,
    dry_run: bool = False,
) -> tuple[str, str]:
    """Apply to a job using Playwright via CDP to an existing Chrome instance.

    Returns:
        (status, log_text)
        status: 'applied' | 'captcha' | 'expired' | 'failed:reason'
    """
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    apply_url = job.get("application_url") or job["url"]
    resume_path = job.get("tailored_resume_path")
    log_lines: list[str] = []

    def log(msg: str) -> None:
        logger.info("[W%d] %s", worker_id, msg)
        log_lines.append(msg)

    # Load user profile
    try:
        profile = config.load_profile()
    except Exception as e:
        return f"failed:profile_load_error", f"Could not load profile: {e}"

    field_data = _build_field_data(profile)

    with sync_playwright() as playwright:
        # Connect to the already-running Chrome via CDP (retry up to 15s)
        import time as _time
        browser = None
        last_err = None
        for _ in range(15):
            try:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                log(f"Connected to Chrome on port {port}")
                break
            except Exception as e:
                last_err = e
                _time.sleep(1)
        if browser is None:
            return "failed:chrome_cdp_error", f"CDP connect failed: {last_err}"

        page = None
        try:
            contexts = browser.contexts
            if contexts:
                ctx = contexts[0]
                pages = ctx.pages
                page = pages[0] if pages else ctx.new_page()
            else:
                ctx = browser.new_context()
                page = ctx.new_page()

            page.set_default_timeout(20000)

            # For Workday job pages, navigate directly to the /apply sub-URL
            # to bypass the "click Apply" step and land on the sign-in/form page.
            nav_url = apply_url
            if "myworkdayjobs.com" in apply_url.lower():
                if not any(s in apply_url.lower() for s in ["/apply", "/login", "/signin"]):
                    nav_url = apply_url.rstrip("/") + "/apply"
                    log(f"Workday job detected — navigating directly to apply URL: {nav_url[:100]}")

            log(f"Navigating to: {nav_url}")
            try:
                page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            except PlaywrightTimeout:
                log("Page load timeout")
                return "expired", "\n".join(log_lines)

            # Wait for SPA rendering
            _wait_for_page_ready(page)

            if _detect_captcha(page):
                log("CAPTCHA detected on initial page load")
                return "captcha", "\n".join(log_lines)

            # Log page title and check for expired/404 pages
            try:
                title = page.title()
                log(f"Page title: {title[:80]}")
                url_now = page.url
                if url_now != apply_url:
                    log(f"Redirected to: {url_now[:100]}")
                # Detect expired/removed job postings
                title_lower = title.lower()
                expired_signals = [
                    "404", "not found", "page not found", "job not found",
                    "position not found", "no longer available", "job closed",
                    "posting expired", "listing not found", "error"
                ]
                if any(s in title_lower for s in expired_signals):
                    log(f"Job appears expired or removed (title: {title[:60]})")
                    return "expired", "\n".join(log_lines)
            except Exception:
                pass

            # Detect if we got redirected away from the specific job (e.g. Workday → homepage)
            try:
                current_url = page.url
                # If we ended up on a different domain or the URL changed significantly
                from urllib.parse import urlparse
                orig_parsed = urlparse(apply_url)
                curr_parsed = urlparse(current_url)
                if (orig_parsed.netloc == curr_parsed.netloc and
                        orig_parsed.path != curr_parsed.path and
                        len(curr_parsed.path) < len(orig_parsed.path) // 2):
                    log(f"Redirected to different path — job likely expired (was: {orig_parsed.path[:60]}, now: {curr_parsed.path[:60]})")
                    return "expired", "\n".join(log_lines)
            except Exception:
                pass

            # Handle Workday login wall (may appear before or after clicking Apply)
            _handle_workday_login(page, log)
            _wait_for_page_ready(page)

            # If this is a job description page (no form yet), click the Apply CTA
            if not _has_form_elements(page):
                log("No form found — looking for Apply CTA button")
                if _click_apply_cta(page):
                    _wait_for_page_ready(page, timeout_ms=10000)
                    page.wait_for_timeout(2500)  # Extra wait for Workday SPA to render login form
                    # Check if Apply opened a new tab (common for Workday)
                    try:
                        all_pages = ctx.pages
                        if len(all_pages) > 1:
                            newest = all_pages[-1]
                            if newest != page:
                                log(f"Apply opened new tab — switching to: {newest.url[:80]}")
                                page = newest
                                page.set_default_timeout(20000)
                                _wait_for_page_ready(page)
                    except Exception as _e:
                        log(f"New-tab check error: {_e}")
                    # Handle Workday login wall that may appear after clicking Apply
                    _handle_workday_login(page, log)
                    _wait_for_page_ready(page)
                    if _detect_captcha(page):
                        return "captcha", "\n".join(log_lines)
                else:
                    log("No Apply CTA found on description page")
                    # Log visible buttons for debugging
                    try:
                        btns = page.query_selector_all("button, [role='button'], a")
                        visible_btns = []
                        for b in btns[:25]:
                            if b.is_visible():
                                txt = (b.inner_text() or b.get_attribute("aria-label") or "").strip()[:40]
                                if txt:
                                    visible_btns.append(txt)
                        if visible_btns:
                            log(f"Visible buttons on page: {visible_btns[:10]}")
                            # Check if this looks like a job search homepage (no Apply button)
                            homepage_signals = ["search for jobs", "browse jobs", "explore careers",
                                                "find jobs", "job search", "back to jobs"]
                            btn_text_combined = " ".join(visible_btns).lower()
                            if any(s in btn_text_combined for s in homepage_signals):
                                log("Page looks like careers homepage — job expired/redirected")
                                return "expired", "\n".join(log_lines)
                        else:
                            log("No visible buttons — job may be blocked or expired")
                            return "expired", "\n".join(log_lines)
                    except Exception:
                        pass

            resume_uploaded = False

            # Multi-step form loop (up to 50 steps — Workday can be very long)
            _wd_signin_attempts = 0  # Track consecutive sign-in attempts (fail-fast after 3)
            _last_page_hash = ""    # Stagnation detector: page content hash
            _stagnant_steps = 0     # Consecutive steps with no page change
            for step in range(1, 151):
                try:
                    log(f"--- Step {step} [url: {page.url[:80]}] ---")
                except Exception:
                    log(f"--- Step {step} ---")

                # Detect Workday application-level sign-in form.
                # Triggers on "Create Account / Sign In" step of the application.
                # signInSubmitButton is always aria-hidden — clicking it opens the sign-in
                # options modal (Apple / Google / Email). We click "Sign in with email"
                # (SignInWithEmailButton), wait for email+password to expand, fill via
                # React-compatible JS, then JS-click signInSubmitButton to submit.
                try:
                    _wd_signin_btn = page.locator(
                        "[data-automation-id='signInSubmitButton']"
                    ).first
                    _app_signin_visible = False
                    try:
                        _app_signin_visible = _wd_signin_btn.is_visible(timeout=1000)
                    except Exception:
                        pass
                    if not _app_signin_visible:
                        # Sign-in button gone → successfully past sign-in step
                        if _wd_signin_attempts > 0:
                            log(f"  Sign-in form gone — logged in successfully")
                        _wd_signin_attempts = 0
                    if _app_signin_visible:
                        import os as _os
                        _wd_email = _os.environ.get("WORKDAY_EMAIL", "")
                        _wd_pw = _os.environ.get("WORKDAY_PASSWORD", "")
                        if not _wd_email or not _wd_pw:
                            log(f"  WORKDAY_EMAIL/PASSWORD not set — skipping app-level sign-in")
                        else:
                            _wd_signin_attempts += 1
                            log(f"  App-level Workday sign-in on step {step} (attempt {_wd_signin_attempts})")
                            # After 3 consecutive sign-in attempts with no progress, try create-account or fail
                            if _wd_signin_attempts > 3:
                                log(f"  Sign-in stuck after {_wd_signin_attempts} attempts — trying create-account or aborting")
                                _ca_done = False
                                try:
                                    _ca_btn = page.locator(
                                        "[data-automation-id='createAccountLink'], "
                                        "[data-automation-id='createAccountButton'], "
                                        "button:has-text('Create Account'), a:has-text('Create Account'), "
                                        "button:has-text('Create an account'), a:has-text('Create an account')"
                                    ).first
                                    if _ca_btn.is_visible(timeout=800):
                                        log(f"    Create Account button found — attempting")
                                        _ca_btn.click(force=True)
                                        page.wait_for_timeout(1500)
                                        _ca_done = True
                                except Exception:
                                    pass
                                if not _ca_done:
                                    return "failed:workday_auth_error", "\n".join(log_lines)
                                # Reset counter after create-account click so it doesn't re-trigger immediately
                                _wd_signin_attempts = 0
                            # Step A: click "Sign in with email" DIRECTLY on the page (not via modal)
                            # SignInWithEmailButton is directly visible at y~838, before any modal.
                            # Clicking it expands an inline email+password form.
                            _email_btn_clicked = False
                            for _email_sel in [
                                "[data-automation-id='SignInWithEmailButton']",
                                "button:has-text('Sign in with email')",
                                "button:has-text('Use email')",
                            ]:
                                try:
                                    _email_btn = page.locator(_email_sel).first
                                    if _email_btn.is_visible(timeout=1500):
                                        _email_btn.click(force=True)
                                        page.wait_for_timeout(2000)
                                        log(f"    Clicked 'Sign in with email' ({_email_sel})")
                                        _email_btn_clicked = True
                                        break
                                except Exception:
                                    pass
                            if not _email_btn_clicked:
                                # Fallback: open the options modal and click email button inside it
                                try:
                                    _wd_signin_btn.click(force=True, timeout=2000)
                                    page.wait_for_timeout(1000)
                                    for _ms in ["[data-automation-id='SignInWithEmailButton']",
                                                "button:has-text('Sign in with email')"]:
                                        try:
                                            _mb = page.locator(_ms).first
                                            if _mb.is_visible(timeout=1000):
                                                _mb.click(force=True)
                                                page.wait_for_timeout(1500)
                                                log(f"    Clicked email btn via modal fallback ({_ms})")
                                                _email_btn_clicked = True
                                                break
                                        except Exception:
                                            pass
                                except Exception as _e:
                                    log(f"    Modal fallback error: {_e}")
                            if not _email_btn_clicked:
                                log(f"    'Sign in with email' button not found anywhere")

                            # Step C: fill email + password using React-compatible setter
                            try:
                                _js_fill_result = page.evaluate("""([emailVal, pwVal]) => {
                                    function reactSet(el, val) {
                                        const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                                        if (desc && desc.set) desc.set.call(el, val);
                                        else el.value = val;
                                        ['input', 'change'].forEach(t =>
                                            el.dispatchEvent(new Event(t, { bubbles: true }))
                                        );
                                    }
                                    const emailEl = document.querySelector(
                                        "[data-automation-id='email'] input, " +
                                        "input[type='email'], input[autocomplete='email'], " +
                                        "input[name='email']"
                                    );
                                    const pwEl = document.querySelector(
                                        "input[type='password'], [data-automation-id='password']"
                                    );
                                    if (emailEl) reactSet(emailEl, emailVal);
                                    if (pwEl) reactSet(pwEl, pwVal);
                                    return `email=${!!emailEl},pw=${!!pwEl}`;
                                }""", [_wd_email, _wd_pw])
                                log(f"    React-fill: {_js_fill_result}")
                                page.wait_for_timeout(500)
                            except Exception as _e:
                                log(f"    React-fill error: {_e}")

                            # Step D: submit — find the inline email-form submit button
                            # After clicking "sign in with email", an email+password form expands.
                            # Its submit button is NOT signInSubmitButton (which opens options modal).
                            # Log all buttons to identify the correct one.
                            try:
                                _js_submit = page.evaluate("""([emailVal, pwVal]) => {
                                    function reactSet(el, val) {
                                        const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                                        if (desc && desc.set) desc.set.call(el, val);
                                        else el.value = val;
                                        ['input', 'change'].forEach(t =>
                                            el.dispatchEvent(new Event(t, { bubbles: true }))
                                        );
                                    }
                                    // Re-fill credentials to make sure they're current
                                    const emailEl = document.querySelector(
                                        "input[type='email'], input[autocomplete='email'], input[name='email']"
                                    );
                                    const pwEl = document.querySelector("input[type='password']");
                                    if (emailEl) reactSet(emailEl, emailVal);
                                    if (pwEl) reactSet(pwEl, pwVal);

                                    // Find all visible submit buttons and their positions
                                    const allBtns = Array.from(document.querySelectorAll(
                                        "button[type='submit'], input[type='submit']"
                                    ));
                                    const btnInfo = allBtns.map(b => {
                                        const r = b.getBoundingClientRect();
                                        return `y=${Math.round(r.y)} aid=${b.getAttribute('data-automation-id')||''} t="${(b.textContent||b.value||'').trim().slice(0,20)}"`;
                                    }).join(' | ');

                                    // Click the LOWEST positioned submit button (form button, not nav)
                                    // EXCLUDE signInSubmitButton — it's the ghost modal-opener that
                                    // would dismiss the inline email form we just expanded.
                                    let best = null, bestY = -1;
                                    for (const b of allBtns) {
                                        const aid = b.getAttribute('data-automation-id') || '';
                                        if (aid === 'utilityButtonSignIn') continue;
                                        if (aid === 'signInSubmitButton') continue;
                                        const r = b.getBoundingClientRect();
                                        if (r.width === 0 || r.height === 0) continue;
                                        if (r.y > bestY) {
                                            bestY = r.y;
                                            best = b;
                                        }
                                    }
                                    if (best) {
                                        best.click();
                                        return 'clicked_y' + Math.round(bestY) + ':' + (best.textContent||best.value||'').trim().slice(0,20) + ' | btns:' + btnInfo;
                                    }
                                    return 'not_found | btns:' + btnInfo;
                                }""", [_wd_email, _wd_pw])
                                log(f"    Submit result: {_js_submit}")
                                page.wait_for_timeout(1000)
                            except Exception as _e:
                                log(f"    Submit error: {_e}")

                            # Press Enter in password field — most reliable way to submit inline sign-in form
                            try:
                                _pw_inp = page.locator("input[type='password']").first
                                if _pw_inp.is_visible(timeout=800):
                                    _pw_inp.focus()
                                    page.wait_for_timeout(200)
                                    _pw_inp.press("Enter")
                                    log(f"    Pressed Enter in password field")
                                    page.wait_for_timeout(4000)
                                else:
                                    page.wait_for_timeout(3000)
                            except Exception as _e:
                                log(f"    Enter-press error: {_e}")
                                page.wait_for_timeout(3000)

                            # Check for sign-in errors
                            _signin_error = None
                            try:
                                _err_el = page.locator(
                                    "[data-automation-id='errorMessage'], "
                                    "[aria-live='assertive']:not(:empty), "
                                    "[class*='error']:visible"
                                ).first
                                if _err_el.is_visible(timeout=1000):
                                    _signin_error = (_err_el.inner_text() or "").strip()[:150]
                                    log(f"    Sign-in error: '{_signin_error}'")
                            except Exception:
                                pass

                            _wait_for_page_ready(page)
                            log(f"  App-level sign-in attempt complete")

                            # If credentials are wrong / account locked, try create-account first
                            # (handles first-time users who have no existing Workday account)
                            if _signin_error and any(kw in _signin_error.lower() for kw in
                                    ["wrong email", "wrong password", "locked", "incorrect",
                                     "invalid", "not found", "does not exist"]):
                                _ca_done = False
                                try:
                                    _ca_btn_loc = page.locator(
                                        "[data-automation-id='createAccountLink'], "
                                        "[data-automation-id='createAccountButton'], "
                                        "button:has-text('Create Account'), a:has-text('Create Account'), "
                                        "button:has-text('Create an account'), a:has-text('Create an account'), "
                                        "a:has-text('Register'), button:has-text('Register')"
                                    )
                                    _verify_loc = page.locator(
                                        "[data-automation-id='verifyPassword'], "
                                        "[data-automation-id='confirmPassword']"
                                    )
                                    _has_ca = False
                                    try:
                                        _has_ca = (
                                            _ca_btn_loc.first.is_visible(timeout=800) or
                                            _verify_loc.first.is_visible(timeout=800)
                                        )
                                    except Exception:
                                        pass
                                    if _has_ca:
                                        log(f"    Create Account option detected — attempting account creation")
                                        try:
                                            if _ca_btn_loc.first.is_visible(timeout=500):
                                                _ca_btn_loc.first.click(force=True)
                                                page.wait_for_timeout(1500)
                                                log(f"    Clicked Create Account button")
                                        except Exception:
                                            pass
                                        # Fill all create-account fields via React-compatible setter
                                        _ca_fill = page.evaluate("""([emailVal, pwVal, fn, ln]) => {
                                            function reactSet(el, val) {
                                                const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                                                if (desc && desc.set) desc.set.call(el, val);
                                                else el.value = val;
                                                ['input', 'change'].forEach(t =>
                                                    el.dispatchEvent(new Event(t, { bubbles: true }))
                                                );
                                            }
                                            // Fill ALL password inputs (password + confirm/verify)
                                            const pwInputs = document.querySelectorAll("input[type='password']");
                                            for (const pw of pwInputs) reactSet(pw, pwVal);
                                            // Fill email
                                            const emailEl = document.querySelector(
                                                "input[type='email'], [data-automation-id='email'] input, " +
                                                "input[autocomplete='email'], input[name='email']"
                                            );
                                            if (emailEl) reactSet(emailEl, emailVal);
                                            // Fill name fields if present
                                            const fnEl = document.querySelector(
                                                "[data-automation-id='firstName'] input, " +
                                                "[data-automation-id='legalName--firstName'] input, " +
                                                "input[name='firstName'], input[id*='firstName']"
                                            );
                                            if (fnEl) reactSet(fnEl, fn);
                                            const lnEl = document.querySelector(
                                                "[data-automation-id='lastName'] input, " +
                                                "[data-automation-id='legalName--lastName'] input, " +
                                                "input[name='lastName'], input[id*='lastName']"
                                            );
                                            if (lnEl) reactSet(lnEl, ln);
                                            return `email=${!!emailEl},fn=${!!fnEl},ln=${!!lnEl},pwCount=${pwInputs.length}`;
                                        }""", [_wd_email, _wd_pw,
                                               field_data.get("first_name", ""),
                                               field_data.get("last_name", "")])
                                        log(f"    Create account fill: {_ca_fill}")
                                        page.wait_for_timeout(500)
                                        # Submit the create-account form
                                        _ca_submit = page.evaluate("""() => {
                                            const btns = Array.from(document.querySelectorAll(
                                                "button[type='submit'], input[type='submit'], " +
                                                "[data-automation-id='createAccountButton']"
                                            ));
                                            const info = btns.map(b =>
                                                `aid=${b.getAttribute('data-automation-id')||''} t="${(b.textContent||b.value||'').trim().slice(0,20)}"`
                                            ).join(' | ');
                                            // Prefer buttons with "create"/"register"/"sign up" text
                                            for (const b of btns) {
                                                const t = (b.textContent || b.value || '').toLowerCase();
                                                if (t.includes('create') || t.includes('register') || t.includes('sign up')) {
                                                    b.click();
                                                    return 'create:' + t.slice(0, 20) + ' | ' + info;
                                                }
                                            }
                                            // Fallback: lowest positioned visible submit button
                                            let best = null, bestY = -1;
                                            for (const b of btns) {
                                                const r = b.getBoundingClientRect();
                                                if (r.width > 0 && r.height > 0 && r.y > bestY) {
                                                    bestY = r.y; best = b;
                                                }
                                            }
                                            if (best) {
                                                best.click();
                                                return 'lowest:' + (best.textContent||'').trim().slice(0,20) + ' | ' + info;
                                            }
                                            return 'not_found | ' + info;
                                        }""")
                                        log(f"    Create account submit: {_ca_submit}")
                                        page.wait_for_timeout(4000)
                                        _wait_for_page_ready(page)
                                        # Check for errors on the create-account form
                                        _ca_error = None
                                        try:
                                            _ca_err_el = page.locator(
                                                "[data-automation-id='errorMessage'], "
                                                "[aria-live='assertive']:not(:empty)"
                                            ).first
                                            if _ca_err_el.is_visible(timeout=1000):
                                                _ca_error = (_ca_err_el.inner_text() or "").strip()[:150]
                                                log(f"    Create account error: '{_ca_error}'")
                                        except Exception:
                                            pass
                                        if not _ca_error:
                                            log(f"    Create account succeeded — continuing")
                                            _ca_done = True
                                except Exception as _ca_ex:
                                    log(f"    Create account attempt error: {_ca_ex}")

                                if not _ca_done:
                                    log(f"  Workday credentials failed — aborting (check WORKDAY_EMAIL/PASSWORD)")
                                    return "failed:workday_auth_error", "\n".join(log_lines)

                            continue
                except Exception as _app_signin_err:
                    log(f"  App sign-in detection error: {_app_signin_err}")

                n_text = _fill_text_inputs(page, field_data)
                n_sel = _fill_selects(page, field_data)
                n_radio = _fill_radio_checkboxes(page, field_data)
                log(f"  Filled: {n_text} text, {n_sel} selects, {n_radio} radio groups")
                for _d in _wd_fill_diag:
                    log(f"    {_d}")

                # Stagnation check: if page content hasn't changed for 10 consecutive steps, abort
                try:
                    import hashlib as _hashlib
                    _page_body = page.inner_text("body", timeout=2000)[:800]
                    _cur_hash = _hashlib.md5(_page_body.encode()).hexdigest()
                    if _cur_hash == _last_page_hash:
                        _stagnant_steps += 1
                        if _stagnant_steps >= 10:
                            log(f"  Page unchanged for {_stagnant_steps} steps — aborting (stuck)")
                            return "failed:stuck_loop", "\n".join(log_lines)
                        elif _stagnant_steps >= 3:
                            log(f"  Warning: page unchanged for {_stagnant_steps} steps")
                    else:
                        _stagnant_steps = 0
                    _last_page_hash = _cur_hash
                except Exception:
                    pass

                if resume_path and not resume_uploaded:
                    if _upload_resume(page, resume_path):
                        resume_uploaded = True
                        log(f"  Resume uploaded: {Path(resume_path).name}")
                        page.wait_for_timeout(1000)

                if _detect_captcha(page):
                    log("CAPTCHA detected — cannot continue")
                    return "captcha", "\n".join(log_lines)

                # Log all visible buttons for diagnosis (first 5 steps only)
                if step <= 5 or step % 20 == 0:
                    try:
                        all_vis_btns = page.query_selector_all(
                            "button, [role='button'], input[type='submit']"
                        )
                        vis_btn_info = []
                        for _b in all_vis_btns[:30]:
                            try:
                                if _b.is_visible():
                                    _t = (_b.inner_text() or _b.get_attribute("value") or "").strip()[:30]
                                    _bt = _b.get_attribute("type") or ""
                                    _aid = _b.get_attribute("data-automation-id") or ""
                                    _box = _b.bounding_box() or {}
                                    vis_btn_info.append(
                                        f"'{_t}' type={_bt} aid={_aid} y={int(_box.get('y', -1))}"
                                    )
                            except Exception:
                                pass
                        log(f"  Visible buttons: {vis_btn_info[:10]}")
                    except Exception:
                        pass
                    # Also log page heading/text summary
                    try:
                        headings = page.query_selector_all("h1, h2, h3, [data-automation-id*='header'], [data-automation-id*='title'], [data-automation-id*='heading']")
                        heading_texts = []
                        for _h in headings[:5]:
                            try:
                                if _h.is_visible():
                                    _ht = (_h.inner_text() or "").strip()[:60]
                                    if _ht:
                                        heading_texts.append(_ht)
                            except Exception:
                                pass
                        if heading_texts:
                            log(f"  Page headings: {heading_texts}")
                    except Exception:
                        pass

                action, btn = _find_submit_or_next(page)
                try:
                    btn_text = (btn.inner_text() or btn.get_attribute("value") or "").strip()[:30] if btn else ""
                    log(f"  Next action: {action} (btn: '{btn_text}')")
                except Exception:
                    log(f"  Next action: {action}")

                if action == "none":
                    # Check if we're on a success page already
                    if _detect_success(page):
                        log("Success page detected (no button needed)")
                        return "applied", "\n".join(log_lines)
                    # Fallback: try Apply CTA click before giving up.
                    # This handles the case where we're still on the job description
                    # page and the Apply button wasn't matched by _find_submit_or_next.
                    log("No submit/next button found — trying Apply CTA as fallback")
                    if _click_apply_cta(page):
                        _wait_for_page_ready(page, timeout_ms=10000)
                        page.wait_for_timeout(2000)
                        try:
                            all_pages = ctx.pages
                            if len(all_pages) > 1:
                                newest = all_pages[-1]
                                if newest != page:
                                    log(f"  Apply CTA opened new tab — switching to: {newest.url[:80]}")
                                    page = newest
                                    page.set_default_timeout(20000)
                                    _wait_for_page_ready(page)
                        except Exception as _nte:
                            log(f"  New-tab check error: {_nte}")
                        _handle_workday_login(page, log)
                        _wait_for_page_ready(page)
                        continue
                    # Also try a broader JS click on any visible "Apply" button as last resort
                    log("  CTA fallback failed — trying JS broad apply button search")
                    try:
                        _js_apply = page.evaluate("""() => {
                            const kws = ['apply now', 'apply for this job', 'apply to this job',
                                         'easily apply', 'easy apply', 'quick apply',
                                         'apply on company site', 'start application', 'apply'];
                            const els = Array.from(document.querySelectorAll(
                                'button, a, [role="button"], input[type="button"], input[type="submit"]'
                            ));
                            for (const kw of kws) {
                                for (const el of els) {
                                    const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').toLowerCase().trim();
                                    if (t === kw || t.startsWith(kw)) {
                                        const r = el.getBoundingClientRect();
                                        if (r.width > 0 && r.height > 0) {
                                            el.click();
                                            return 'clicked:' + t.slice(0, 40);
                                        }
                                    }
                                }
                            }
                            return 'not_found';
                        }""")
                        log(f"  JS apply search: {_js_apply}")
                        if _js_apply != "not_found":
                            _wait_for_page_ready(page, timeout_ms=10000)
                            page.wait_for_timeout(2000)
                            try:
                                all_pages = ctx.pages
                                if len(all_pages) > 1:
                                    newest = all_pages[-1]
                                    if newest != page:
                                        log(f"  JS Apply opened new tab — switching to: {newest.url[:80]}")
                                        page = newest
                                        page.set_default_timeout(20000)
                                        _wait_for_page_ready(page)
                            except Exception:
                                pass
                            _handle_workday_login(page, log)
                            _wait_for_page_ready(page)
                            continue
                    except Exception as _jse:
                        log(f"  JS apply search error: {_jse}")
                    log("All Apply button strategies exhausted — no submit button found")
                    return "failed:no_submit_button", "\n".join(log_lines)

                elif action == "next":
                    try:
                        btn.click(timeout=10000)
                    except Exception:
                        try:
                            btn.click(force=True, timeout=10000)
                        except Exception as click_e:
                            log(f"  Next button click error: {click_e}")
                    page.wait_for_timeout(2000)
                    # Check if Next opened a new tab (some ATS redirect to external forms)
                    try:
                        all_pages = ctx.pages
                        if len(all_pages) > 1:
                            newest = all_pages[-1]
                            if newest != page:
                                log(f"  Next opened new tab — switching to: {newest.url[:80]}")
                                page = newest
                                page.set_default_timeout(20000)
                                _wait_for_page_ready(page)
                    except Exception:
                        pass
                    continue

                elif action == "submit":
                    if dry_run:
                        log("[DRY RUN] Would click submit — stopping here")
                        return "dry_run", "\n".join(log_lines)

                    try:
                        btn.click(timeout=10000)
                    except Exception:
                        try:
                            btn.click(force=True, timeout=10000)
                        except Exception as click_e:
                            log(f"  Submit click error: {click_e} — trying JS click")
                            try:
                                page.evaluate("btn => btn.click()", btn)
                            except Exception:
                                pass
                    log("  Clicked submit button")

                    # Wait for navigation or confirmation
                    try:
                        page.wait_for_timeout(4000)
                    except Exception:
                        pass

                    # Check if submit opened a new tab (e.g. Indeed → external ATS)
                    try:
                        all_pages = ctx.pages
                        if len(all_pages) > 1:
                            newest = all_pages[-1]
                            if newest != page:
                                log(f"  Submit opened new tab — switching to: {newest.url[:80]}")
                                page = newest
                                page.set_default_timeout(20000)
                                _wait_for_page_ready(page)
                                _handle_workday_login(page, log)
                                _wait_for_page_ready(page)
                    except Exception:
                        pass

                    if _detect_captcha(page):
                        log("CAPTCHA appeared after submit")
                        return "captcha", "\n".join(log_lines)

                    if _detect_success(page):
                        log("Application submitted successfully!")
                        return "applied", "\n".join(log_lines)

                    # Maybe another confirmation step
                    action2, btn2 = _find_submit_or_next(page)
                    if action2 == "submit" and btn2:
                        try:
                            btn2.click(timeout=10000)
                        except Exception:
                            try:
                                btn2.click(force=True, timeout=10000)
                            except Exception:
                                try:
                                    page.evaluate("b => b.click()", btn2)
                                except Exception:
                                    pass
                        page.wait_for_timeout(3000)
                        if _detect_success(page):
                            log("Application submitted (confirmation step)")
                            return "applied", "\n".join(log_lines)

                    # No explicit confirmation — continue loop (multi-step form)
                    continue

            return "failed:too_many_steps", "\n".join(log_lines)

        except PlaywrightTimeout as e:
            log(f"Playwright timeout: {e}")
            return "expired", "\n".join(log_lines)
        except Exception as e:
            log(f"Playwright error ({type(e).__name__}): {e}")
            return f"failed:playwright_{type(e).__name__}", "\n".join(log_lines)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            # Do NOT close the browser — Chrome lifecycle is managed by chrome.py
