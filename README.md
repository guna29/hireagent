<![CDATA[<div align="center">

# HireAgent

### AI-Powered Job Discovery, Resume Tailoring & Application Pipeline

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge)](LICENSE)
[![LLM](https://img.shields.io/badge/LLM-NVIDIA%20NIM%20%2B%20Ollama-76b900?style=for-the-badge&logo=nvidia&logoColor=white)](https://build.nvidia.com)
[![Browser](https://img.shields.io/badge/Browser-Playwright-2EAD33?style=for-the-badge&logo=playwright&logoColor=white)](https://playwright.dev)
[![ATS](https://img.shields.io/badge/ATS-Greenhouse%20%7C%20Lever%20%7C%20Workday-6366f1?style=for-the-badge)]()

**Built by [Gunakarthik Naidu Lanka](https://www.linkedin.com/in/gunakarthik-naidu-lanka) — MS Computer Science, Arizona State University**

*From job boards to submitted applications — fully automated.*

</div>

---

## What Is HireAgent?

HireAgent is a production-grade agentic pipeline that automates the entire job search workflow for entry-level software engineers. It scrapes **8 job boards**, scores every role with an LLM, generates a **unique tailored resume** (LaTeX → PDF) per job, and pre-fills ATS application forms via a **real browser agent** — all from a single command.

Built specifically for H-1B/OPT candidates targeting US-based software engineering roles.

---

## Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         HIREAGENT PIPELINE                          │
└─────────────────────────────────────────────────────────────────────┘

  Stage 1: DISCOVER          Stage 2: ENRICH          Stage 3: SCORE
  ─────────────────          ───────────────          ──────────────
  ┌─────────────┐            ┌─────────────┐          ┌────────────┐
  │  LinkedIn   │            │  Visit each │          │ DeepSeek   │
  │  Indeed     │ ─────────► │  job page   │ ───────► │    V3      │
  │  Glassdoor  │            │  Pull full  │          │  1–10 fit  │
  │  ZipRecruit │            │  desc +     │          │  score     │
  │  Workday    │            │  apply URL  │          └────────────┘
  │  YC WaaS    │            └─────────────┘
  │  Wellfound  │
  │  Otto.ai    │
  └─────────────┘

  Stage 4: TAILOR            Stage 5: COVER           Stage 6: APPLY
  ───────────────            ──────────────           ─────────────
  ┌─────────────┐            ┌─────────────┐          ┌────────────┐
  │ DeepSeek R1 │            │ DeepSeek R1 │          │ Playwright │
  │ selects     │ ─────────► │ generates   │ ───────► │ browser    │
  │ layout +    │            │ targeted    │          │ agent      │
  │ rewrites    │            │ cover       │          │ fills ATS  │
  │ bullets     │            │ letter      │          │ forms      │
  │             │            └─────────────┘          │            │
  │ LaTeX → PDF │                                     │ Greenhouse │
  └─────────────┘                                     │ Lever      │
                                                      │ Workday    │
                                                      │ LinkedIn   │
                                                      └────────────┘
```

---

## Key Features

### Multi-Source Job Discovery
Aggregates listings from 8 sources simultaneously:

| Source | Type | Focus |
|--------|------|-------|
| LinkedIn | General | Broad reach, Easy Apply |
| Indeed | General | Volume |
| Glassdoor | General | Salary data |
| ZipRecruiter | General | Startup-heavy |
| Workday Portals | Corporate | Direct ATS integration |
| YC Work at a Startup | Startup | H-1B-friendly YC companies |
| Wellfound | Startup | AngelList startup ecosystem |
| Otto.careers | AI-focused | AI/ML engineering roles |

### Intelligent LLM Routing
| Stage | Model | Why |
|-------|-------|-----|
| Enrich | Ollama gemma3:4b | Fast local extraction |
| Score | NVIDIA DeepSeek-V3 | High reasoning, ATS-style matching |
| Tailor / Cover | NVIDIA DeepSeek-R1 | Long-form writing quality |
| Form Fill | NVIDIA Llama-3.1-70B | Structured JSON output for form data |
| Select | Claude Haiku | Bullet ranking |

All NVIDIA stages fall back to local Ollama automatically — **zero cloud dependency required.**

### Eligibility Classification Engine
Every job passes through a rule-based classifier before any LLM call:

```
✗ Senior / Staff / Principal / Lead / Director
✗ PhD or Security Clearance required
✗ Non-US location (Canada, UK, India, etc.)
✗ Hardware / Mechanical / Civil / RF Engineering
✗ Internships and co-ops
✗ 2+ years experience explicitly required
✓ Entry-level, New Grad, Junior, Associate
✓ US-based or Remote (any US state)
✓ Software / Backend / Frontend / ML / Platform
```

### Resume Tailoring Engine
For every eligible job:
1. LLM selects the optimal resume layout (experience-heavy vs. project-heavy)
2. Scores all experience bullets and projects by keyword overlap with the JD
3. Rewrites selected bullets to mirror JD language using **real metrics only** (no hallucination)
4. Compiles to a clean one-page PDF via LaTeX

**Anti-hallucination rules baked in:**
- No fabricated companies, degrees, or certifications
- All metrics must be from `profile.json:real_metrics`
- No em dashes, no first-person pronouns
- Banned words enforced by `validator.py`

### ATS Form Filling
Browser agent handles 5 ATS platforms:

| ATS | Strategy | Notes |
|-----|----------|-------|
| Greenhouse | React Select + placeholder-based | Country/Location comboboxes, LLM fallback |
| Lever | Standard inputs | Fast-fill mode |
| Workday | Standard + ARIA | Multi-step form navigation |
| LinkedIn Easy Apply | Answer history + resume upload | Dropdown memory |
| Generic HTML | Label-matching + LLM fallback | Covers 80%+ of remaining forms |

Unknown fields are resolved via **LLM batch inference** (NVIDIA Llama-3.1-70B) — the agent describes all unlabeled fields and gets fill values in a single API call.

---

## Live Dashboard

When running `hireagent apply`, a real-time Rich dashboard shows:

```
                         HireAgent Dashboard
┏━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ W  ┃ Job                          ┃ Status         ┃ Score ┃ Last Action            ┃
┡━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 0  │ Software Engineer / Meta     │ APPLYING       │  9/10 │ Filling work experience│
│ 1  │ Backend SWE / Stripe         │ TAILORING      │  8/10 │ Generating PDF...      │
└────┴──────────────────────────────┴────────────────┴───────┴────────────────────────┘
╭────────────────── Recent Events ──────────────────╮
│ 14:23:01 [W0] Applied: SmithRx — Greenhouse ✓     │
│ 14:21:43 [W0] Applied: Figma — LinkedIn Easy ✓    │
│ 14:19:12 [W1] Skipped: senior role (seniority)    │
╰───────────────────────────────────────────────────╯
```

---

## Tech Stack

```
Language         Python 3.11+
LLM Providers    NVIDIA NIM (DeepSeek-V3, DeepSeek-R1, Llama-3.1-70B) + Ollama (local)
Browser Agent    Playwright (Chromium CDP)
PDF Generation   LaTeX (pdflatex / Carlito font)
Database         SQLite (WAL mode, concurrent-safe)
CLI              Typer + Rich
Job Scraping     python-jobspy, custom HTTP/GraphQL scrapers
Notifications    Telegram Bot API
Config           YAML + JSON + .env
```

---

## Quick Start

### Prerequisites
- Python 3.11+
- `pdflatex` ([TeX Live](https://www.tug.org/texlive/))
- Google Chrome
- Ollama (for free local LLM)

```bash
# 1. Install TeX Live
brew install --cask mactex-no-gui   # macOS
# sudo apt-get install texlive-full  # Linux

# 2. Install Ollama + pull a model
brew install ollama
ollama pull gemma3:4b

# 3. Clone and install
git clone https://github.com/guna29/hireagent.git
cd hireagent
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .

# 4. Configure
mkdir -p ~/.hireagent
cp profile.example.json ~/.hireagent/profile.json
# Edit ~/.hireagent/profile.json with your info

# 5. Set API keys (optional — only needed for cloud LLMs)
cat > ~/.hireagent/.env << EOF
NVIDIA_API_KEY=your_nvidia_nim_key      # Free tier at build.nvidia.com
ANTHROPIC_API_KEY=your_key_here         # Optional, for Claude Haiku bullet select
HIREAGENT_SKIP_SMART_EXTRACT=1          # Faster discovery
EOF

# 6. Initialize
hireagent doctor
```

### Run the Pipeline

```bash
# Full pipeline: discover → score → tailor → apply
hireagent run

# Individual stages
hireagent run discover       # Scrape all 8 job boards
hireagent run enrich         # Pull full job descriptions
hireagent run score          # LLM fit scoring (1-10)
hireagent run tailor         # Generate tailored resumes
hireagent run cover          # Generate cover letters
hireagent run pdf            # Compile LaTeX → PDF

# Start applying
hireagent apply              # Apply continuously, score ≥7
hireagent apply --dry-run    # Fill forms but don't submit

# Pipeline status
hireagent status

# View job queue
hireagent view
```

---

## Configuration

### `~/.hireagent/profile.json`
Your personal profile — experiences, projects, skills, real metrics. See [`profile.example.json`](profile.example.json) for the full schema.

### `~/.hireagent/searches.yaml`
Control which job boards, queries, and locations to search:

```yaml
sites: [linkedin, indeed, zip_recruiter, glassdoor]

queries:
  - query: "software engineer new grad"
    tier: 1
  - query: "backend engineer entry level"
    tier: 1
  - query: "junior software developer"
    tier: 2

locations:
  - location: "United States"
    remote: true
  - location: "San Francisco, CA"
    remote: false
```

### `~/.hireagent/.env`
```env
# LLM (cloud — optional)
NVIDIA_API_KEY=nvapi-...          # Free tier: build.nvidia.com
ANTHROPIC_API_KEY=sk-ant-...      # Optional

# LLM (local — always free)
OLLAMA_BASE_URL=http://localhost:11434
LLM_MODEL=llama3:8b
ENRICH_LLM_MODEL=gemma3:4b

# Optional
CAPSOLVER_API_KEY=...             # Captcha solving
HIREAGENT_TELEGRAM_TOKEN=...      # Telegram notifications
HIREAGENT_TELEGRAM_CHAT_ID=...
HIREAGENT_SKIP_SMART_EXTRACT=1    # Skip AI scraper (faster)
```

---

## How Scoring Works

The LLM evaluates each job across 4 dimensions:

| Dimension | Weight | What It Checks |
|-----------|--------|----------------|
| Keyword Match | 30 pts | Exact + semantic term overlap between resume and JD |
| Achievement Density | 30 pts | 5 pts per bullet with a quantified metric |
| Seniority Alignment | 20 pts | Project complexity vs. expected experience level |
| Recency | 20 pts | Core skills from last 12 months |

Score returned as **1–10**. Jobs below 7 are discovered and tailored but skipped during apply.

---

## Project Structure

```
hireagent/
├── src/hireagent/
│   ├── cli.py                     # CLI entry point (Typer)
│   ├── pipeline.py                # Stage orchestrator
│   ├── eligibility.py             # Rule-based job classifier
│   ├── database.py                # SQLite layer (WAL, concurrent-safe)
│   ├── config.py                  # .env / profile.json / searches.yaml loader
│   ├── llm.py                     # Multi-LLM router (NVIDIA NIM + Ollama)
│   ├── latex_renderer.py          # LaTeX → PDF compiler
│   ├── resume_rotation.py         # Active resume management
│   ├── telegram_bot.py            # Telegram notifications
│   │
│   ├── discovery/
│   │   ├── jobspy.py              # LinkedIn, Indeed, Glassdoor, ZipRecruiter
│   │   ├── workday.py             # Corporate Workday JSON API scraper
│   │   ├── workatastartup.py      # YC Work at a Startup scraper
│   │   ├── wellfound.py           # Wellfound (AngelList) GraphQL scraper
│   │   ├── otto.py                # Otto.careers AI jobs scraper
│   │   ├── scoutbetter.py         # ScoutBetter private API
│   │   └── smartextract.py        # AI-powered fallback scraper
│   │
│   ├── enrichment/
│   │   └── detail.py              # Full JD + apply URL fetcher
│   │
│   ├── scoring/
│   │   ├── scorer.py              # LLM fit scoring (1-10)
│   │   ├── tailor.py              # Resume tailoring + layout selection
│   │   ├── cover_letter.py        # Cover letter generation
│   │   ├── pdf.py                 # Batch PDF compilation
│   │   └── validator.py           # Anti-hallucination + banned word checks
│   │
│   ├── apply/
│   │   ├── launcher.py            # Apply queue + job acquisition
│   │   ├── free_agent.py          # ATS form-filling engine (Playwright)
│   │   ├── prompt.py              # Agent system prompt builder
│   │   └── chrome.py              # Chrome CDP session manager
│   │
│   ├── templates/
│   │   └── fixed_resume.tex       # LaTeX resume base template
│   │
│   └── config/
│       ├── searches.example.yaml
│       ├── employers.yaml         # Workday employer registry
│       └── sites.yaml
│
├── profile.example.json           # Profile template (fill with your info)
├── pyproject.toml
├── LICENSE
└── README.md
```

---

## Debug Commands

```bash
# Pipeline health check
hireagent doctor

# View all jobs
hireagent view
hireagent view --status tailored
hireagent view --min-score 8

# Database info
hireagent debug db-info
hireagent debug jobs --pending-apply
hireagent debug jobs --failed-only

# Retry failed applications
hireagent apply --reset-failed

# Apply to a specific job
hireagent apply --url "https://boards.greenhouse.io/company/job/123"
```

---

## About the Author

**Gunakarthik Naidu Lanka** — MS Computer Science, Arizona State University (GPA 4.0)

- LinkedIn: [linkedin.com/in/gunakarthik-naidu-lanka](https://www.linkedin.com/in/gunakarthik-naidu-lanka)
- GitHub: [github.com/guna29](https://github.com/guna29)
- Portfolio: [gunakarthik-naidu-lanka-portfolio.vercel.app](https://gunakarthik-naidu-lanka-portfolio.vercel.app)

---

## License

MIT © 2026 Gunakarthik Naidu Lanka — see [LICENSE](LICENSE)

---

<div align="center">
<sub>Built with Python, Playwright, NVIDIA NIM, LaTeX, and a lot of job applications.</sub>
</div>
]]>