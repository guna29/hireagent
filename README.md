<div align="center">

# 🤖 HireAgent

### AI-Powered Job Discovery, Resume Tailoring & Auto-Application Pipeline

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge)](LICENSE)
[![LLM](https://img.shields.io/badge/LLM-NVIDIA%20NIM%20%2B%20Ollama-76b900?style=for-the-badge&logo=nvidia&logoColor=white)](https://build.nvidia.com)
[![Browser](https://img.shields.io/badge/Automation-Playwright-2EAD33?style=for-the-badge&logo=playwright&logoColor=white)](https://playwright.dev)
[![ATS](https://img.shields.io/badge/ATS-Greenhouse%20%7C%20Lever%20%7C%20Workday-6366f1?style=for-the-badge)]()

**Built by [Gunakarthik Naidu Lanka](https://www.linkedin.com/in/gunakarthik-naidu-lanka)**
MS Computer Science @ Arizona State University • GPA 4.0

*From job boards → tailored resume → submitted application. Fully automated.*

</div>

---

## 🚀 What Is HireAgent?

HireAgent is a **production-grade agentic pipeline** that automates the entire job hunt for entry-level software engineers.

It scrapes **8 job boards**, scores every role with an LLM, generates a **unique tailored one-page resume** (LaTeX → PDF) per job, and fills out ATS application forms using a **real browser agent** — all from a single terminal command.

> Built specifically for **H-1B / OPT candidates** targeting US software engineering roles.

---

## ⚡ How It Works — The Pipeline

```
🔍 DISCOVER  →  📋 ENRICH  →  🧠 SCORE  →  ✏️ TAILOR  →  📄 PDF  →  🖱️ APPLY
```

| Stage | What Happens | Model Used |
|-------|-------------|------------|
| 🔍 **Discover** | Scrapes 8 job boards for entry-level SWE roles | — |
| 📋 **Enrich** | Visits each job page, pulls full description + apply URL | Ollama gemma3:4b |
| 🧠 **Score** | Rates each job 1–10 (keyword match, seniority, tech stack) | NVIDIA DeepSeek-V3 |
| ✏️ **Tailor** | Rewrites resume bullets to mirror the JD language | NVIDIA DeepSeek-R1 |
| 📄 **Cover** | Generates a targeted cover letter | NVIDIA DeepSeek-R1 |
| 🖨️ **PDF** | Compiles tailored resume → one-page PDF via LaTeX | — |
| 🖱️ **Apply** | Opens real browser, fills ATS form, submits | NVIDIA Llama-3.1-70B |

---

## 🌐 Job Sources — 8 Boards Scraped

| Source | Type | Why It Matters |
|--------|------|----------------|
| 🔵 **LinkedIn** | General | Broad reach + Easy Apply |
| 🟡 **Indeed** | General | Highest volume |
| 🟢 **Glassdoor** | General | Salary data included |
| 🟠 **ZipRecruiter** | General | Startup-heavy listings |
| 🏢 **Workday Portals** | Corporate ATS | Direct company career pages |
| 🚀 **YC Work at a Startup** | Startup | H-1B-friendly YC companies |
| 💼 **Wellfound** | Startup | AngelList startup ecosystem |
| 🤖 **Otto.careers** | AI-focused | AI/ML engineering roles |

---

## 🧠 Multi-LLM Routing

HireAgent uses the **right model for each task** — and falls back to free local Ollama if cloud APIs are unavailable:

```
Scoring   →  NVIDIA DeepSeek-V3        (cloud, high reasoning)
Tailoring →  NVIDIA DeepSeek-R1-14B    (cloud, long-form writing)
Cover     →  NVIDIA DeepSeek-R1-14B    (cloud, long-form writing)
Form Fill →  NVIDIA Llama-3.1-70B      (cloud, structured JSON)
Enrich    →  Ollama gemma3:4b          (local, fast extraction)
─────────────────────────────────────────────────────────────
All NVIDIA stages → auto-fallback to local Ollama if API unavailable
= 100% free operation with no API keys required
```

---

## 🛡️ Eligibility Filter — No Wasted Applications

Every job passes through a **rule-based classifier** before any LLM touches it:

```
❌ BLOCKED                          ✅ ACCEPTED
──────────────────────────────────  ──────────────────────────────────
Senior / Staff / Principal / Lead   Entry-level / New Grad / Junior
PhD or Security Clearance required  BS/MS roles — no clearance needed
Non-US location (Canada, UK, India) US-based or Remote (all 50 states)
Hardware / Mechanical / RF Eng      Software / Backend / Frontend / ML
Internships and co-ops              Full-time positions only
Requires 2+ years experience        0–1 year experience or unspecified
```

---

## 📄 Resume Tailoring Engine

For every eligible job, HireAgent:

1. **Selects the optimal layout** — experience-heavy (3 exp + 2 proj) or project-heavy (2 exp + 3 proj) based on the JD
2. **Scores all bullets** — ranks experiences and projects by keyword overlap with the job description
3. **Rewrites bullets** — mirrors the JD language using **real metrics only** (no hallucination)
4. **Compiles to PDF** — LaTeX → one-page PDF with Carlito 10pt, 0.5in margins

**Anti-hallucination rules enforced by `validator.py`:**
- ❌ No fabricated companies, degrees, or certifications
- ❌ No metrics not in your `profile.json:real_metrics`
- ❌ No em dashes, no first-person pronouns
- ❌ Banned words checked on every compile

---

## 🖱️ ATS Form Filling — 5 Platforms Supported

| ATS | Strategy |
|-----|----------|
| **Greenhouse** | React Select comboboxes + placeholder-based fills + LLM fallback |
| **Lever** | Standard input matching, fast-fill mode |
| **Workday** | ARIA-label navigation, multi-step form handling |
| **LinkedIn Easy Apply** | Dropdown answer memory + resume upload |
| **Generic HTML** | Label-matching + LLM batch inference for unknown fields |

> Unknown fields are resolved via **LLM batch inference** — the agent describes all unlabeled fields and gets answers in a single NVIDIA API call.

---

## 📊 Live Terminal Dashboard

```
                         🤖 HireAgent Dashboard
┏━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ W  ┃ Job                          ┃ Status       ┃ Score ┃ Last Action            ┃
┡━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 0  │ Software Engineer / Meta     │ ✅ APPLYING  │  9/10 │ Filling work history   │
│ 1  │ Backend SWE / Stripe         │ ✏️ TAILORING │  8/10 │ Generating PDF...      │
└────┴──────────────────────────────┴──────────────┴───────┴────────────────────────┘
╭─────────────────────── Recent Events ──────────────────────────╮
│ 14:23:01 [W0] ✅ Applied: SmithRx — Greenhouse                 │
│ 14:21:43 [W0] ✅ Applied: Figma — LinkedIn Easy Apply          │
│ 14:19:12 [W1] ⏭️  Skipped: senior role detected                │
╰────────────────────────────────────────────────────────────────╯
```

---

## 🛠️ Tech Stack

| Category | Tools |
|----------|-------|
| **Language** | Python 3.11+ |
| **LLM Cloud** | NVIDIA NIM — DeepSeek-V3, DeepSeek-R1-14B, Llama-3.1-70B |
| **LLM Local** | Ollama — gemma3:4b, llama3:8b |
| **Browser Agent** | Playwright (Chromium CDP) |
| **PDF Generation** | LaTeX / pdflatex (Carlito font) |
| **Database** | SQLite (WAL mode, concurrent-safe) |
| **CLI / UI** | Typer + Rich |
| **Job Scraping** | python-jobspy + custom HTTP/GraphQL scrapers |
| **Notifications** | Telegram Bot API |
| **Config** | YAML + JSON + .env |

---

## ⚙️ Quick Start

### 1. Install Prerequisites
```bash
# macOS
brew install --cask mactex-no-gui   # LaTeX for PDF generation
brew install ollama && ollama pull gemma3:4b

# Linux
sudo apt-get install texlive-full
```

### 2. Clone & Install
```bash
git clone https://github.com/guna29/hireagent.git
cd hireagent
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 3. Set Up Your Profile
```bash
mkdir -p ~/.hireagent
cp profile.example.json ~/.hireagent/profile.json
# ✏️ Edit ~/.hireagent/profile.json with your info
```

### 4. Add API Keys (Optional — Everything Works Free with Ollama)
```bash
cat > ~/.hireagent/.env << EOF
NVIDIA_API_KEY=nvapi-...       # Free tier: build.nvidia.com
ANTHROPIC_API_KEY=sk-ant-...   # Optional
HIREAGENT_SKIP_SMART_EXTRACT=1 # Faster discovery
EOF
```

### 5. Run
```bash
hireagent doctor          # Verify setup
hireagent run             # Full pipeline: discover → tailor
hireagent apply           # Start applying (score ≥ 7)
```

---

## 📋 All Commands

```bash
# Pipeline stages
hireagent run                    # Full pipeline
hireagent run discover           # Scrape job boards
hireagent run enrich             # Pull full descriptions
hireagent run score              # LLM fit scoring
hireagent run tailor             # Generate tailored resumes
hireagent run cover              # Generate cover letters
hireagent run pdf                # Compile to PDF

# Applying
hireagent apply                  # Apply continuously, score ≥ 7
hireagent apply --dry-run        # Fill forms but don't submit
hireagent apply --reset-failed   # Retry failed applications
hireagent apply --url <url>      # Apply to one specific job

# Monitoring
hireagent status                 # Pipeline stats
hireagent view                   # Browse job queue
hireagent doctor                 # Health check
```

---

## 📁 Project Structure

```
hireagent/
├── 📄 profile.example.json         ← Fill this with your info
├── ⚙️  pyproject.toml
│
└── src/hireagent/
    ├── 🖥️  cli.py                   ← All CLI commands
    ├── 🔄 pipeline.py              ← Stage orchestrator
    ├── 🛡️  eligibility.py          ← Job classifier (entry-level, US, SWE)
    ├── 🗄️  database.py             ← SQLite layer
    ├── 🤖 llm.py                   ← Multi-LLM router (NVIDIA + Ollama)
    ├── 📊 latex_renderer.py        ← LaTeX → PDF compiler
    │
    ├── discovery/
    │   ├── jobspy.py               ← LinkedIn, Indeed, Glassdoor, ZipRecruiter
    │   ├── workday.py              ← Corporate Workday portals
    │   ├── workatastartup.py       ← YC Work at a Startup
    │   ├── wellfound.py            ← Wellfound / AngelList
    │   └── otto.py                 ← Otto.careers AI jobs
    │
    ├── scoring/
    │   ├── scorer.py               ← LLM fit scoring (1–10)
    │   ├── tailor.py               ← Resume tailoring + layout
    │   ├── cover_letter.py         ← Cover letter generation
    │   └── validator.py            ← Anti-hallucination checks
    │
    └── apply/
        ├── free_agent.py           ← ATS form-filling engine
        ├── launcher.py             ← Apply queue manager
        └── chrome.py               ← Chrome CDP session
```

---

## 🙋 About the Author

**Gunakarthik Naidu Lanka** — MS Computer Science, Arizona State University (GPA 4.0)

Currently seeking entry-level Software Engineering roles (H-1B/OPT sponsorship).

[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-0077B5?style=flat-square&logo=linkedin)](https://www.linkedin.com/in/gunakarthik-naidu-lanka)
[![GitHub](https://img.shields.io/badge/GitHub-guna29-181717?style=flat-square&logo=github)](https://github.com/guna29)
[![Portfolio](https://img.shields.io/badge/Portfolio-Visit-22c55e?style=flat-square)](https://gunakarthik-naidu-lanka-portfolio.vercel.app)

---

## 📜 License

MIT © 2026 Gunakarthik Naidu Lanka — see [LICENSE](LICENSE)

---

<div align="center">
<sub>⚡ Built with Python, Playwright, NVIDIA NIM, LaTeX, and way too many job applications.</sub>
</div>
