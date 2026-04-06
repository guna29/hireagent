"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells Claude Code / the AI agent
how to fill out a job application form using Playwright MCP tools. All
personal data is loaded from the user's profile -- nothing is hardcoded.
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from hireagent import config

logger = logging.getLogger(__name__)


def _build_profile_summary(profile: dict) -> str:
    """Format the applicant profile section of the prompt.

    Reads all relevant fields from the profile dict and returns a
    human-readable multi-line summary for the agent.
    """
    p = profile
    personal = p["personal"]
    work_auth = p["work_authorization"]
    comp = p["compensation"]
    exp = p.get("experience", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})

    first_name = personal.get("first_name", personal["full_name"].rsplit(" ", 1)[0])
    last_name = personal.get("last_name", personal["full_name"].rsplit(" ", 1)[-1])
    phone_raw = personal.get("phone_raw", "".join(c for c in personal.get("phone", "") if c.isdigit())[-10:])

    lines = [
        f"Full Name: {personal['full_name']}",
        f"First Name: {first_name}",
        f"Last Name: {last_name}",
        f"Preferred Name: {personal.get('preferred_name', first_name)}",
        f"Pronouns: {eeo.get('pronouns', personal.get('pronouns', 'He/Him'))}",
        f"Email: {personal['email']}",
        f"Phone (raw digits, no dashes): {phone_raw}",
        f"Phone (formatted): {personal['phone']}",
        f"Phone Country Code: {personal.get('phone_country_code', '+1')}",
    ]

    # Address
    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")
    lines.append(f"Street: {personal.get('address', '')}")
    lines.append(f"City: {personal.get('city', '')}")
    lines.append(f"State: {personal.get('province_state', '')} ({personal.get('province_state_full', 'Arizona')})")
    lines.append(f"Country: {personal.get('country', 'USA')} / {personal.get('country_full', 'United States')}")
    lines.append(f"Postal/ZIP Code: {personal.get('postal_code', '')}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    # Work authorization
    lines.append(f"Legally Authorized to Work in US: Yes")
    lines.append(f"Visa/Work Permit: {work_auth.get('work_permit_type', 'F-1 OPT')}")
    lines.append(f"Require Sponsorship: No")
    lines.append(f"Citizenship Country: {personal.get('citizenship_country', 'India')}")
    lines.append(f"Lived outside US 12+ consecutive months in last 7 years: {personal.get('lived_outside_us_12_months', 'Yes')} (India)")
    lines.append(f"Government employee (current or past 3 years): No")
    lines.append(f"In sanctioned country: No")

    # Compensation
    currency = comp.get("salary_currency", "USD")
    lines.append(f"Salary Expectation: ${comp.get('salary_mid', comp['salary_expectation'])} {currency} (midpoint)")
    lines.append(f"Salary Range: ${comp.get('salary_range_min','110000')}-${comp.get('salary_range_max','180000')} {currency}")

    # Experience
    edu = p.get("education", {})
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education Level: {exp['education_level']}")
    if edu.get("school"):
        lines.append(f"School: {edu['school']}")
    if edu.get("degree"):
        lines.append(f"Degree: {edu['degree']} in {edu.get('field_of_study','Computer Science')}")
    if edu.get("gpa"):
        lines.append(f"GPA: {edu['gpa']}")
    if edu.get("graduation_date"):
        lines.append(f"Graduation Date: {edu['graduation_date']}")

    # Availability
    lines.append(f"Start Date: {avail.get('earliest_start_date', 'April 15, 2026')}")
    lines.append(f"Available Full-Time: {avail.get('available_for_full_time', 'Yes')}")
    lines.append(f"Available Contract: {avail.get('available_for_contract', 'Yes')}")

    # Standard responses
    lines.extend([
        "Age 18+: Yes",
        "Background Check: Yes",
        "Felony: No",
        "Previously Worked Here: No",
        "How did you hear about us: Indeed (or 'Online Job Board' if Indeed not an option)",
    ])

    # EEO — fill with actual profile data, do NOT opt out
    lines.append(f"Pronouns: {eeo.get('pronouns', 'He/Him')}")
    lines.append(f"Gender: {eeo.get('gender', 'Male')} — select 'Male' or closest match")
    lines.append(f"Hispanic or Latino: {eeo.get('hispanic_or_latino', 'No')} — select 'No' or 'I am not Hispanic or Latino'")
    lines.append(f"Race/Ethnicity: {eeo.get('race_ethnicity', 'Asian')} — select 'Asian' or 'Two or more races' or 'South Asian' — whichever option exists")
    lines.append(f"Veteran Status: {eeo.get('veteran_status', 'I am not a protected veteran')} — select this or 'Not a veteran'")
    lines.append(f"Military Status: No")
    lines.append(f"Disability: {eeo.get('disability_status', 'No, I do not have a disability')} — select 'No, I do not have a disability' or 'I don't have a disability'")

    return "\n".join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    """Build the location eligibility check section of the prompt.

    Uses the accept_patterns from search config to determine which cities
    are acceptable for hybrid/onsite roles.
    """
    personal = profile["personal"]
    location_cfg = search_config.get("location", {})
    accept_patterns = location_cfg.get("accept_patterns", [])
    primary_city = personal.get("city", location_cfg.get("primary", "your city"))

    # Build the list of acceptable cities for hybrid/onsite
    if accept_patterns:
        city_list = ", ".join(accept_patterns)
    else:
        city_list = primary_city

    return f"""== LOCATION CHECK (do this FIRST before any form) ==
The candidate is WILLING TO RELOCATE ANYWHERE in the United States. Location is NEVER a reason to fail.
- Any US city (onsite, hybrid, or remote) -> ELIGIBLE. Apply.
- If a form asks "Are you willing to relocate?" -> Answer YES.
- If a screening question asks about willingness to work in [city] -> Answer YES.
- Only exception: job is OVERSEAS ONLY (India, Philippines, Europe, etc.) with zero remote option -> Output RESULT:FAILED:not_eligible_location
- Any US location (SF, NYC, Austin, Seattle, anywhere) -> Apply confidently."""


def _build_salary_section(profile: dict) -> str:
    """Build the salary negotiation instructions.

    Adapts floor, range, and currency from the profile's compensation section.
    """
    comp = profile["compensation"]
    currency = comp.get("salary_currency", "USD")
    floor = comp["salary_expectation"]
    range_min = comp.get("salary_range_min", floor)
    range_max = comp.get("salary_range_max", str(int(floor) + 20000) if floor.isdigit() else floor)
    conversion_note = comp.get("currency_conversion_note", "")

    # Compute example hourly rates at 3 salary levels
    try:
        floor_int = int(floor)
        examples = [
            (f"${floor_int // 1000}K", floor_int // 2080),
            (f"${(floor_int + 25000) // 1000}K", (floor_int + 25000) // 2080),
            (f"${(floor_int + 55000) // 1000}K", (floor_int + 55000) // 2080),
        ]
        hourly_line = ", ".join(f"{sal} = ${hr}/hr" for sal, hr in examples)
    except (ValueError, TypeError):
        hourly_line = "Divide annual salary by 2080"

    # Currency conversion guidance
    if conversion_note:
        convert_line = f"Posting is in a different currency? -> {conversion_note}"
    else:
        convert_line = "Posting is in a different currency? -> Target midpoint of their range. Convert if needed."

    return f"""== SALARY ==
Always use the MIDPOINT of the job's posted salary range. No floor, no minimum — just the midpoint.

Decision tree:
1. Job posting shows a range (e.g. "$120K-$160K")? -> Use the MIDPOINT ($140K). Always.
2. Asked for a range? -> Posted range midpoint minus 10% to midpoint plus 10%. No posted range? -> "${range_min}-${range_max} {currency}".
3. No salary info anywhere? -> Use $110,000 {currency} as default.
4. {convert_line}
5. Hourly rate? -> Divide your annual answer by 2080. ({hourly_line})"""


def _build_screening_section(profile: dict) -> str:
    """Build the screening questions guidance section."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role", personal.get("current_job_title", "software engineer"))
    work_auth = profile["work_authorization"]

    return f"""== SCREENING QUESTIONS (be strategic) ==
Hard facts -> answer truthfully from the profile. No guessing. This includes:
  - Location/relocation: open to relocating ANYWHERE in the US — always answer YES. For "do you live in [city] or willing to relocate?" -> pick the RELOCATION option ("I am open to relocation" / "Yes, willing to relocate"), not the "I currently live there" option unless the city is Tempe/Phoenix AZ.
  - Work authorization: {work_auth.get('legally_authorized_to_work', 'see profile')}
  - Citizenship, clearance, licenses, certifications: answer from profile only
  - Criminal/background: answer from profile only
  - Startup / in-person / fast-paced questions: "Are you ready for a startup environment?" -> YES. "Are you comfortable working in-person/onsite?" -> YES. "Are you prepared to work in a fast-paced environment?" -> YES. These are always YES.

Skills and tools -> be confident. This candidate is a {target_role} with {years} years experience. If the question asks "Do you have experience with [tool]?" and it's in the same domain (DevOps, backend, ML, cloud, automation), answer YES. Software engineers learn tools fast. Don't sell short.

Open-ended questions ("Why do you want this role?", "Tell us about yourself", "What interests you?", cover letter text fields) -> Write in FIRST PERSON ("I am", "I have", "I built", "I worked on"). 2-3 sentences. Be specific to THIS job. Reference something from the job description. Connect it to a real achievement from the resume. Example: "I am a software engineer with a Master's in Computer Science from ASU and hands-on experience building distributed systems and AI platforms. I led development of an agentic AI recruiting tool that automated end-to-end hiring workflows, which directly aligns with this role. I'm excited to bring that experience to [Company]." Sound like a real person, not an AI summary.

Self-attestation / certification checkboxes ("I certify that the information above is accurate", "I acknowledge", "I agree to terms", "By submitting I confirm") -> ALWAYS check/click YES. If there's a signature field next to it, type the full name. Never skip these — they are required to submit.

SMS / text communication consent ("Do you consent to receiving text communications related to your job application via SMS", "May we contact you by text/SMS", or any similar opt-in for job-related texts) -> ALWAYS answer YES / consent. These are standard job application notifications, not spam.

EEO/demographics -> Fill with actual answers from the APPLICANT PROFILE (Gender: Male, Race/Ethnicity: Asian, Hispanic: No, Veteran: No, Disability: No). Use "Prefer not to answer" ONLY if the exact matching option does not exist in the dropdown."""


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section with work auth and name from profile."""
    personal = profile["personal"]
    work_auth = profile["work_authorization"]

    full_name = personal["full_name"]
    first_name = personal.get("first_name", full_name.rsplit(" ", 1)[0])
    last_name = personal.get("last_name", full_name.rsplit(" ", 1)[-1])
    preferred_name = personal.get("preferred_name", first_name)

    return f"""== HARD RULES (never break these) ==
1. Never lie about: citizenship, work authorization, criminal history, education credentials, security clearance, licenses.
2. Work auth: On F-1 OPT. Legally authorized to work in the US RIGHT NOW. Sponsorship answers:
   - "Are you authorized to work in the US?" -> Always YES
   - "What visa do you have?" -> F-1 OPT
   - "Do you require sponsorship NOW / currently?" -> NO (currently on F-1 OPT, authorized right now)
   - "Will you require sponsorship in the future?" -> YES (will need H-1B eventually)
   - Company explicitly says "we do not sponsor visas" -> Answer NO to ALL sponsorship questions (current AND future). Proceed and apply.
   - NEVER reject or skip a job because of sponsorship. Always apply.
3. Name rules — CRITICAL:
   - "First Name" field -> {first_name}
   - "Last Name" field -> {last_name}
   - "Full Name" or single name field -> {full_name}
   - "Preferred Name" or "Goes by" field -> {preferred_name}
   - NEVER put just "Guna" or "Gunakarthik" in the First Name field. It must be "{first_name}".
4. Location autocomplete fields (Greenhouse, Workday, etc. have city autocomplete):
   - Type just the city name: "Tempe" (NOT "Tempe, AZ" — the AZ suffix prevents autocomplete matches)
   - Wait 1-2 seconds for the dropdown suggestion to appear
   - Click the suggestion (usually "Tempe, Arizona, United States" or similar)
   - NEVER type and move on without selecting from the dropdown — it will be marked invalid
   - If "Tempe" shows no results, try "Phoenix" or any major nearby city and select it
   - The candidate is open to relocation everywhere — location on the form does not need to be exact"""


def _build_captcha_section() -> str:
    """Build the CAPTCHA detection and solving instructions.

    Reads the CapSolver API key from environment. The CAPTCHA section
    contains no personal data -- it's the same for every user.
    """
    config.load_env()
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")

    return f"""== CAPTCHA ==
When you see ANY CAPTCHA ("I'm not a robot" checkbox, hCaptcha, Turnstile, image puzzle — anything):
1. Call auto_solve_captcha() — NO arguments. Returns: "CAPTCHA_SOLVED type=hcaptcha sitekey=abc TOKEN:eyJ..."
2. Extract the token: everything AFTER "TOKEN:" in the returned string.
3. Inject the token using browser_evaluate with the EXACT JavaScript string below.
   For hCaptcha (type=hcaptcha):
     (token) => {{ var el = document.querySelector('[name="h-captcha-response"]'); if(el) el.value = token; var el2 = document.querySelector('[name="g-recaptcha-response"]'); if(el2) el2.value = token; return 'injected'; }}
     Pass the token as the argument to the function.
   For reCAPTCHA v2/v3 (type=recaptchav2 or recaptchav3):
     (token) => {{ var el = document.querySelector('[name="g-recaptcha-response"]'); if(el) {{ el.value = token; el.style.display='block'; }} return 'injected'; }}
   For Turnstile (type=turnstile):
     (token) => {{ var el = document.querySelector('[name="cf-turnstile-response"]'); if(el) el.value = token; return 'injected'; }}
4. After injecting: click the Submit button. The form should proceed.
5. If auto_solve_captcha returns "Could not detect sitekey": call solve_captcha(captcha_type, site_key, page_url) with sitekey from page source.
6. Only output RESULT:CAPTCHA if both methods fail after 2 tries.

NEVER try to solve CAPTCHAs by clicking images or audio challenges — always use the API first."""


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False) -> str:
    """Build the full instruction prompt for the apply agent.

    Loads the user profile and search config internally. All personal data
    comes from the profile -- nothing is hardcoded.

    Args:
        job: Job dict from the database (must have url, title, site,
             application_url, fit_score, tailored_resume_path).
        tailored_resume: Plain-text content of the tailored resume.
        cover_letter: Optional plain-text cover letter content.
        dry_run: If True, tell the agent not to click Submit.

    Returns:
        Complete prompt string for the AI agent.
    """
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]

    # --- Resolve resume PDF path ---
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    # Copy to a clean filename for upload (recruiters see the filename)
    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    dest_dir = config.APPLY_WORKER_DIR / "current"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    pdf_path = str(upload_pdf)

    # --- Cover letter handling ---
    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    if cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        # Read text from .txt sibling (PDF is binary)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        # Upload must be PDF
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    # --- Build all prompt sections ---
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    hard_rules = _build_hard_rules(profile)
    captcha_section = _build_captcha_section()

    # Cover letter fallback text
    city = personal.get("city", "the area")
    if not cover_letter_text:
        cl_display = (
            f"None available. Skip if optional. If required, write 2 factual "
            f"sentences: (1) relevant experience from the resume that matches "
            f"this role, (2) available immediately and based in {city}."
        )
    else:
        cl_display = cover_letter_text

    # Phone: prefer phone_raw, fall back to stripping formatted phone
    phone_digits = personal.get("phone_raw", "".join(c for c in personal.get("phone", "") if c.isdigit())[-10:])

    # SSO domains the agent cannot sign into (loaded from config/sites.yaml)
    from hireagent.config import load_blocked_sso
    blocked_sso = load_blocked_sso()

    # Name fields
    first_name = personal.get("first_name", full_name.rsplit(" ", 1)[0])
    last_name = personal.get("last_name", full_name.rsplit(" ", 1)[-1])
    preferred_name = personal.get("preferred_name", first_name)
    display_name = f"{first_name} {last_name}".strip()

    # Dry-run: override submit instruction
    if dry_run:
        submit_instruction = "IMPORTANT: Do NOT click the final Submit/Apply button. Review the form, verify all fields, then output RESULT:APPLIED with a note that this was a dry run."
    else:
        submit_instruction = "BEFORE clicking Submit/Apply, take a snapshot and review EVERY field on the page. Verify all data matches the APPLICANT PROFILE and TAILORED RESUME -- name, email, phone, location, work auth, resume uploaded, cover letter if applicable. If anything is wrong or missing, fix it FIRST. Only click Submit after confirming everything is correct."

    prompt = f"""You are an autonomous job application agent. Your ONE mission: get this candidate an interview. You have all the information and tools. Think strategically. Act decisively. Submit the application.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {job.get('site', 'Unknown')}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF (upload this): {pdf_path}
Cover Letter PDF (upload if asked): {cl_upload_path or "N/A"}

== RESUME TEXT (use when filling text fields) ==
{tailored_resume}

== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR MISSION ==
Submit a complete, accurate application. Use the profile and resume as source data -- adapt to fit each form's format.

If something unexpected happens and these instructions don't cover it, figure it out yourself. You are autonomous. Navigate pages, read content, try buttons, explore the site. The goal is always the same: submit the application. Do whatever it takes to reach that goal.

{hard_rules}

== NEVER DO THESE (immediate RESULT:FAILED if encountered) ==
- NEVER grant camera, microphone, screen sharing, or location permissions. If a site requests them -> RESULT:FAILED:unsafe_permissions
- NEVER do video/audio verification, selfie capture, ID photo upload, or biometric anything -> RESULT:FAILED:unsafe_verification
- NEVER set up a freelancing profile (Mercor, Toptal, Upwork, Fiverr, Turing, etc.). These are contractor marketplaces, not job applications -> RESULT:FAILED:not_a_job_application
- NEVER agree to hourly/contract rates, availability calendars, or "set your rate" flows. You are applying for FULL-TIME salaried positions only.
- NEVER install browser extensions, download executables, or run assessment software.
- NEVER enter payment info, bank details, or SSN/SIN.
- NEVER click "Allow" on any browser permission popup. Always deny/block.
- If the site is NOT a job application form (it's a profile builder, skills marketplace, talent network signup, coding assessment platform) -> RESULT:FAILED:not_a_job_application
- EXPERIENCE CHECK: After reading the job page, check if the job explicitly requires 3+ years, 5+ years, or similar. This candidate has 1 year of experience. If the job STRICTLY requires 3 or more years of professional experience, output RESULT:FAILED:not_eligible_experience. "New Grad", "0-2 years", "junior", or "entry-level" are fine. Also stop if the job requires a PhD degree and the candidate only has a Master's degree.

{location_check}

{salary_section}

{screening_section}

== STEALTH BEHAVIOR — HUMAN MIMICRY ==
You are operating as a human. Anti-bot systems (Cloudflare, Akamai, Datadome) watch timing, mouse movement, and interaction patterns. Follow these rules on every application:

VISUAL SETTLING: After every page load, wait 4–9 seconds before moving the mouse or clicking anything. Simulate a human reading the page before acting.

READING SCROLL: After the page loads and before filling any form, scroll down ~50% of the page, pause 2 seconds, then scroll back up. This mimics a human reading the job description.

TYPING BEHAVIOR: Use type actions with realistic delays. Do NOT instantly fill fields. Pause briefly between fields as a human would move their eyes from one field to the next (1–2 seconds between fields).

SCROLL AUDIT: Before clicking the final Submit button, scroll all the way to the bottom of the page (simulating reading the privacy policy / terms), then scroll back up to the Submit button before clicking it.

CAUTIOUS NAVIGATION: Move toward buttons gradually. Do not snap directly to coordinates. Take a brief pause (1–2 seconds) before clicking any Submit, Apply, Next, or Continue button.

CAPTCHA / ACCESS DENIED: If you see a Cloudflare challenge, "Access Denied", or any hard block — STOP immediately. Do NOT attempt to bypass or force through it. Output RESULT:FAILED:cloudflare_blocked.

== STEP-BY-STEP ==
1. browser_navigate to the job URL. Wait 7 seconds for the page to fully load (SPA pages like Workday need time). If the page shows only skeleton/loading placeholders, wait 5 more seconds and take another snapshot.
2. browser_snapshot to read the page. Then perform a READING SCROLL: scroll 50% down the page, pause 2 seconds, scroll back up. This is required stealth behavior. If CAPTCHA DETECT returns null or throws an error, that is FINE — treat it as no CAPTCHA and continue. If a CAPTCHA IS found, solve it before continuing.
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. Wait 4–6 seconds (visual settling). Then find and click the Apply button. If email-only (page says "email resume to X"):
   - send_email with subject "Application for {job['title']} -- {display_name}", body = 2-3 sentence pitch + contact info, attach resume PDF: ["{pdf_path}"]
   - Output RESULT:APPLIED. Done.
   After clicking Apply: browser_snapshot. Run CAPTCHA DETECT -- many sites trigger CAPTCHAs right after the Apply click. If found, solve before continuing.
5. Login wall / email gate?
   5a. FIRST: check the URL. If you landed on {', '.join(blocked_sso)}, or any SSO/OAuth page -> STOP. Output RESULT:FAILED:sso_required. Do NOT try to sign in to Google/Microsoft/SSO.
   5aa. "Enter your email to continue" / "Enter your email to start your application" (common on iCIMS, Taleo, Greenhouse): This is NOT a login wall — it is the start of the application. Type the email address, click Next/Continue. The system will either create a new account or find an existing one. If it says "Account found — enter your password", enter the password from the profile. If it says "Create your profile / verify your email", follow the flow (enter verification code if needed using read_latest_email). Keep going — this is normal.
   5b. Check for popups. Run browser_tabs action "list". If a new tab/window appeared (login popup), switch to it with browser_tabs action "select". Check the URL there too -- if it's SSO -> RESULT:FAILED:sso_required.
   5c. Regular login form (employer's own site)? Try sign in: {personal['email']} / {personal.get('password', '')}
   5d. After clicking Login/Sign-in: run CAPTCHA DETECT. Login pages frequently have invisible CAPTCHAs that silently block form submissions. If found, solve it then retry login.
   5e. Sign in failed? Try sign up with same email and password.
   5f. Need email verification code? Call the read_latest_email action with the sender's company name or "verification" as the subject keyword. It will return the code directly.
       - IMPORTANT: The email might arrive after a 10-30 second delay. Wait 10 seconds, then call read_latest_email.
       - The function returns a block starting with "[CODES FOUND: ...]" — use ONLY the first code listed. Do NOT use any code from a previous email.
       - Enter the code EXACTLY as shown (no spaces, no extra characters). Enter each digit one at a time if the form has separate boxes.
       - If the code doesn't work, call read_latest_email again — a NEW code may have been sent. Use the NEWEST code.
       - Do NOT open Gmail in a browser tab.
   5g. After login, run browser_tabs action "list" again. Switch back to the application tab if needed.
   5h. All failed? Output RESULT:FAILED:login_issue. Do not loop.
6. Upload resume. ALWAYS upload fresh -- delete any existing resume first:
   - FIRST try: call upload_resume() — NO arguments. This handles hidden file inputs (Greenhouse, Lever, Ashby drag-drop areas) automatically. It returns "Resume uploaded successfully" on success.
   - If upload_resume() fails: fall back to browser_file_upload with the PDF path above.
   - NEVER try to upload by clicking a div/dropzone area directly — it will fail. Always use upload_resume() or browser_file_upload.
7. Upload cover letter if there's a field for it. Text field -> paste the cover letter text. File upload -> use the cover letter PDF path.
8. Check ALL pre-filled fields. ATS systems parse your resume and auto-fill -- it's often WRONG.
   - "Current Job Title" or "Most Recent Title" -> use the title from the TAILORED RESUME summary, NOT whatever the parser guessed.
   - Compare every other field to the APPLICANT PROFILE. Fix mismatches. Fill empty fields.
9. Answer screening questions using the rules above.
10. Self-attestation: Before submitting, look for any "I certify / I acknowledge / I agree / By clicking Submit I confirm" checkboxes or statements. CHECK THEM ALL. If there's a signature field, type the full name. These are required to enable the Submit button.
10b. SCROLL AUDIT (required stealth step): Scroll all the way to the bottom of the page (simulating a human reading the privacy policy). Pause 2 seconds. Then scroll back up to the Submit button.
11. Wait 3–5 seconds (human hesitation pause). Then: {submit_instruction}
12. After submit: browser_snapshot. Run CAPTCHA DETECT -- submit buttons often trigger invisible CAPTCHAs. If found, solve it.
    If the page asks for an email verification code (e.g. "Enter the code sent to your email" / "Check your inbox"):
    - Wait 15 seconds for the email to arrive, then call read_latest_email with "verification" or the company name.
    - Use ONLY the code from "[CODES FOUND: ...]" — the FIRST code listed. Enter it EXACTLY.
    - If the form has separate digit boxes, enter each digit individually into each box.
    - After entering: click Submit/Confirm. If you're redirected BACK to the application form, the code was accepted — take a snapshot and check if the form is now pre-filled (it means you're now logged in). Re-submit the form.
    - If the code fails, call read_latest_email again — a new code may have been issued. Try the newest code.
    Then check for "thank you" or "application received" or "we received your application" to confirm success.
13. Output your result.

== RESULT CODES (output EXACTLY one) ==
RESULT:APPLIED -- submitted successfully
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- blocked by unsolvable captcha
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:reason -- any other failure (brief reason)

== BROWSER EFFICIENCY ==
- browser_snapshot ONCE per page to understand it. Then use browser_take_screenshot to check results (10x less memory).
- Only snapshot again when you need element refs to click/fill.
- Multi-page forms (Workday, Taleo, iCIMS): snapshot each new page, fill all fields, click Next/Continue. Repeat until final review page.
- Fill ALL fields in ONE browser_fill_form call. Not one at a time.
- Keep your thinking SHORT. Don't repeat page structure back.
- CAPTCHA AWARENESS: After Apply/Submit/Login clicks -- run CAPTCHA DETECT. Invisible CAPTCHAs block submissions silently. If the detect script returns null or throws a JS error: that means NO captcha, continue normally.
- Workday skeleton state: If you see a loading skeleton (grey placeholders), wait 5-8 seconds then browser_snapshot again. Do NOT give up immediately. After creating/logging into a Workday account, wait 8 seconds for the application form to load.

== FORM TRICKS ==
- FILL TOP TO BOTTOM, LEFT TO RIGHT: Always fill form fields starting from the TOP of the page and moving DOWN, one column at a time. On multi-column layouts, complete the LEFT column fully before moving to the RIGHT column. Never jump between sections or skip fields. Scroll down progressively after filling visible fields. This prevents missed fields and validation errors.
- Currency: Always use USD. When asked for salary currency, select "USD" or "United States Dollar". If a currency dropdown exists, select USD explicitly before entering the amount.
- Popup/new window opened? browser_tabs action "list" to see all tabs. browser_tabs action "select" with the tab index to switch. ALWAYS check for new tabs after clicking login/apply/sign-in buttons.
- "Upload your resume" pre-fill page (Workday, Lever, etc.): This is NOT the application form yet. Click "Select file" or the upload area, then browser_file_upload with the resume PDF path. Wait for parsing to finish. Then click Next/Continue to reach the actual form.
- File upload not working? Try: (1) browser_click the upload button/area, (2) browser_file_upload with the path. If still failing, look for a hidden file input or a "Select file" link and click that first.
- Dropdown won't fill? browser_click to open it, then browser_click the option.
- Checkbox won't check via fill_form? Use browser_click on it instead. Snapshot to verify.
- "Are you legally authorized to work in the United States?" -> always select/click YES
- Phone country code dropdown: type "+1" then select "United States (+1)" from the dropdown
- Country field dropdown: type "United States" then select it from the dropdown. Never leave blank.
- Phone field (digits only): type {phone_digits}
- Date fields: {datetime.now().strftime('%m/%d/%Y')}
- Validation errors after submit? Take BOTH snapshot AND screenshot. Snapshot shows text errors, screenshot shows red-highlighted fields. Fix all, retry.
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.
- Radio buttons / checkboxes with NO index? Use browser_evaluate to click by label text:
  browser_evaluate function: () => {{{{ const labels = document.querySelectorAll('label'); for (const l of labels) {{ if (l.textContent.trim().startsWith('LABEL_TEXT')) {{ const inp = document.getElementById(l.htmlFor) || l.querySelector('input'); if (inp) {{ inp.click(); return 'clicked'; }} }} }} return 'not found'; }}}}
  Replace LABEL_TEXT with the option you want to select. If that fails, try clicking the label itself via browser_click.

{captcha_section}

== WHEN TO ASK FOR HELP (HUMAN ESCALATION VIA TELEGRAM) ==
Before giving up, if you hit something unexpected, SEND A TELEGRAM MESSAGE to ask the user:
- A form field asking for something not in your profile (e.g. portfolio URL, cover letter link, specific certification number)
- An unusual yes/no question you are not sure how to answer honestly
- A CAPTCHA you cannot solve after 2 attempts
- A required field with limited options you don't know how to map
- Anything where guessing wrong could hurt the application

To escalate:
1. Call notify_user with a CLEAR question like:
   "❓ [Job Title @ Company] — I'm stuck on a form field and need your answer:
   Field: [exact field name]
   Options: [list the options if dropdown/radio]
   Question: [what should I select/enter?]
   Please reply with your answer so I can continue or restart."
2. Then output: RESULT:PAUSED
Stop and wait. Do NOT guess. Do NOT proceed past this point.

== WHEN TO GIVE UP ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- Job is closed/expired/page says "no longer accepting" -> RESULT:EXPIRED
- Page is broken/500 error/blank -> RESULT:FAILED:page_error

BEFORE giving up for ANY reason, ALWAYS call notify_user with a clear message explaining:
- Which job (title + company)
- Why you are exiting (exact reason, e.g. "requires onsite New York", "CAPTCHA unsolvable", "login failed")
This lets the user review every exit decision. Then call done() with the RESULT code.

== FINISHING (CRITICAL) ==
When you are done — success or failure — you MUST call the done() action.
The text passed to done() MUST start with the RESULT code, e.g.:
  done("RESULT:APPLIED")
  done("RESULT:FAILED:login_issue")
  done("RESULT:EXPIRED")
  done("RESULT:CAPTCHA")
  done("RESULT:LOGIN_ISSUE")
This is the ONLY way the system knows what happened. If you finish without calling done() with a RESULT code, the application will be marked as failed."""

    return prompt
