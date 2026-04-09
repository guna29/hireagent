<div align="center">

# 🤖 HireAgent

### AI-Powered Job Discovery, Resume Tailoring & Auto-Application Pipeline

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge)](LICENSE)
[![LLM](https://img.shields.io/badge/LLM-NVIDIA%20NIM-76b900?style=for-the-badge&logo=nvidia&logoColor=white)](https://build.nvidia.com)
[![Browser](https://img.shields.io/badge/Automation-Playwright-2EAD33?style=for-the-badge&logo=playwright&logoColor=white)](https://playwright.dev)
[![ATS](https://img.shields.io/badge/ATS-Workday%20%7C%20Greenhouse%20%7C%20Lever-6366f1?style=for-the-badge)]()

**Built by [Gunakarthik Naidu Lanka](https://www.linkedin.com/in/gunakarthik-naidu-lanka)**
MS Computer Science @ Arizona State University • GPA 4.0

*From job boards → tailored resume → submitted application. Fully automated.*

</div>

---

## 🚀 What Is HireAgent?

HireAgent is a **production-grade agentic pipeline** that automates the entire job hunt for entry-level software engineers.

It scrapes **8+ job boards**, scores every role with an LLM, generates a **unique tailored one-page resume** (LaTeX → PDF) per job, and fills out ATS application forms using a **Vision-Verified Browser Agent** — all from a single terminal command.

---

## ⚡ How It Works — The Pipeline

| Stage | What Happens | Model Used |
|-------|-------------|------------|
| 🔍 **Discover** | Scrapes 8 job boards for entry-level SWE roles | — |
| 📋 **Enrich** | Visits each job page, pulls full description + apply URL | Ollama gemma3:4b |
| 🧠 **Score** | Rates each job 1–10 (relevance, tech stack) | NVIDIA DeepSeek-V3 |
| ✏️ **Tailor** | Rewrites resume bullets to mirror the JD language | NVIDIA DeepSeek-R1 |
| 📄 **Cover** | Generates a targeted cover letter | NVIDIA DeepSeek-R1 |
| 🖨️ **PDF** | Compiles tailored resume → one-page PDF via LaTeX | — |
| 🖱️ **Apply** | **Vision-verified fill** + Enterprise CAPTCHA Solving | **NVIDIA Llama-3.2-Vision** |

---

## 🦾 Autonomous Application Engine (`Apply`)

The application engine has been upgraded for maximum resilience:

### 1. Vision-Verified Filling (SoM)
Using a **Set-of-Marks (SoM)** approach, HireAgent takes a screenshot of the form, draws bounding boxes around every interactive element, and uses **Llama-3.2-11B-Vision** to perfectly map your profile data to the visual layout.

### 2. Enterprise CAPTCHA Solving
Integrated with **CapSolver API** to automatically solve:
- 🧩 **hCaptcha**: Specialist detection for Lever/Veeva formats.
- 🌀 **Cloudflare Turnstile**: Handles script & iframe injected challenges.
- 🤖 **reCAPTCHA v2 / Enterprise**: Sitekey-based solving and injection.
- 🛡️ **Arkose Labs / FunCaptcha**: Enterprise-grade solving.

### 3. Smarter Navigation & Gates
- **Email-Gate Bypass**: Automatically completes "Submit email to enter" pre-forms (iCIMS/Breezy).
- **SSO False-Positive Protection**: Smart detection that only skips if no form exists, avoiding global navigation false alarms.
- **LinkedIn Logic**: Prioritizes direct ATS applications over LinkedIn Apply for higher success rates.

---

## 🧠 Multi-LLM Routing

HireAgent uses the **right model for each task**:

```
Scoring   →  NVIDIA DeepSeek-V3        (Cloud, high reasoning)
Tailoring →  NVIDIA DeepSeek-R1-14B    (Cloud, long-form writing)
Vision    →  NVIDIA Llama-3.2-11B-Vis  (Cloud, visual field mapping)
Mapping   →  NVIDIA Nemotron-340B      (Cloud, complex JSON schemas)
Enrich    →  Ollama gemma3:4b          (Local, fast extraction)
```

---

## 🛡️ Eligibility Filter — No Wasted Applications

Every job passes through a **rule-based classifier**:
- ✅ BS/MS roles (Entry-level / New Grad / Junior)
- ✅ US-based or Remote
- ❌ Senior / Staff / Principal / Lead
- ❌ Non-US location
- ❌ Requires Security Clearance

---

## ⚙️ Quick Start

### 1. Install Prerequisites
```bash
# macOS
brew install --cask mactex-no-gui   # LaTeX for PDF generation
brew install ollama && ollama pull gemma3:4b
```

### 2. Set Up API Keys
```bash
cat > ~/.hireagent/.env << EOF
NVIDIA_API_KEY=nvapi-...       # build.nvidia.com
CAPSOLVER_API_KEY=CAP-...      # capsolver.com
TELEGRAM_BOT_TOKEN=...         # For real-time unknown fields
TELEGRAM_CHAT_ID=...           # Notification destination
EOF
```

### 3. Run
```bash
hireagent doctor            # Verify setup
hireagent run               # Full pipeline: discover → tailor
hireagent apply --limit 5    # Start autonomous application
```

---

## 📋 Commands

```bash
hireagent apply                  # Apply continuously, score ≥ 7
hireagent apply --dry-run        # Fill forms but don't submit
hireagent apply --url <url>      # Apply to one specific job
```

---

## 📁 Project Structure

```
hireagent/
└── src/hireagent/
    ├── 🖥️  cli.py               ← All CLI commands
    ├── 📊 latex_renderer.py    ← LaTeX → PDF compiler
    │
    └── apply/
        ├── free_agent.py       ← Core state machine (SSO, Navigation)
        ├── vision_loop.py      ← Vision Fill + CAPTCHA Solver
        ├── launcher.py         ← Worker queue & Purge logic
        └── chrome.py           ← Playwright session manager
```

---

## 🙋 About the Author

**Gunakarthik Naidu Lanka** — MS Computer Science, Arizona State University (GPA 4.0)

[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-0077B5?style=flat-square&logo=linkedin)](https://www.linkedin.com/in/gunakarthik-naidu-lanka)
[![GitHub](https://img.shields.io/badge/GitHub-guna29-181717?style=flat-square&logo=github)](https://github.com/guna29)

---

## 📜 License

MIT © 2026 Gunakarthik Naidu Lanka — see [LICENSE](LICENSE)

<div align="center">
<sub>⚡ Built with Python, Playwright, NVIDIA NIM, LaTeX, and way too many job applications.</sub>
</div>
