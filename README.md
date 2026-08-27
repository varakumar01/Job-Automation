# Varakumar G — Resume Workspace

## Files

| File | Purpose |
|---|---|
| `varakumar_resume.tex` | **Master resume** — edit here first |
| `LINKEDIN_SUGGESTIONS.md` | Prioritised LinkedIn profile fixes |
| `varakumar_s_resume__AI_.pdf` | Previous resume (reference, do not edit) |
| `reference resume.png` | Template reference (Silicon Valley format) |
| `linkedIN profile.pdf` | LinkedIn export (reference, do not edit) |

---

## Quick Preview (Local, 2 commands)

```bash
# 1. Install tectonic once  (self-contained TeX engine, ~50 MB download)
! sudo pacman -S tectonic

# 2. Build + open PDF in okular
make preview
```

> **okular** stays open — when you save edits to the `.tex` file, just run
> `make preview` again (or `make build` to recompile without reopening the viewer).

| Command | Does |
|---|---|
| `make` or `make build` | Compile `.tex` → `varakumar_resume.pdf` |
| `make preview` | Compile then open in okular |
| `make clean` | Delete the compiled PDF and build artefacts |
| `make check` | Verify tectonic is installed |

---

## Compile the Resume

### Option A — Overleaf (Recommended, zero setup)

1. Go to [overleaf.com](https://www.overleaf.com) → **New Project → Upload Project**
2. Upload `varakumar_resume.tex`
3. Set compiler to **pdfLaTeX** (Menu → Compiler → pdfLaTeX)
4. Click **Recompile** — the PDF appears on the right
5. Download with the **Download PDF** button

All packages used (`charter`, `geometry`, `enumitem`, `hyperref`, `microtype`)
are built into Overleaf — nothing to install.

### Option B — Local (TeX Live / MiKTeX)

```bash
# First compile (builds .aux etc.)
pdflatex varakumar_resume.tex

# Second compile (resolves any cross-references — safe to skip for this resume)
pdflatex varakumar_resume.tex
```

Output: `varakumar_resume.pdf` in the same directory.

---

## Making Edits

### Quick content changes

Open `varakumar_resume.tex` in any text editor (or Overleaf's editor).
Sections are clearly commented — find the section you want with Ctrl+F on the section name.

### Adding a new job / project

Copy the pattern:

```latex
\textbf{Company Name} | Job Title \hfill Location | Start -- End
\begin{itemize}
  \item Achievement 1
  \item Achievement 2
\end{itemize}
```

### Adding a new section

Use the `\resumeSection{}` macro — it draws the bold uppercase title + ruled line
matching the rest of the document:

```latex
\resumeSection{New Section Name}

Content goes here...
```

---

## Before You Send (Checklist)

- [ ] Search for `CONFIRM` in the `.tex` file — there is one line flagging Metasploit.
      Keep it if you've used it; remove it if you haven't.
- [ ] Replace `(In Progress)` next to OSCP once you pass.
- [ ] Update LinkedIn to match (see `LINKEDIN_SUGGESTIONS.md` — items 1 & 2 are critical).
- [ ] Check `portfolio.dragnux.com` is live and current.

---

## Sector Résumé Variants

`varakumar_resume.tex` is the master — sacred, never auto-written. Six committed,
hand-authored sector bases live under `resumes/`, each a full copy of the master with
only the **lens** changed per PLAN.md's fact-atom model (same facts/numbers/entities,
different foregrounded aspect — see `resumes/facts.json` for the fact ledger and
PLAN.md §9 2026-08-24 for the design decisions):

| Sector | File | Leads with |
|---|---|---|
| VAPT / Pentest | `resumes/vapt.tex` | Independent Assessment (Bill Manager, PortSwigger, AD) + OSCP/TCM |
| Cloud Security | `resumes/cloud.tex` | CloudSploit end-to-end across AWS/Azure/GCP/Oracle + CIS benchmarks |
| OT/ICS Security | `resumes/ot-ics.tex` | 2,800+ CVEs, six vendor families, Modbus/BACnet/DNP3/PROFINET depth |
| AppSec / API Security | `resumes/appsec.tex` | Bill Manager GHSA, crAPI/VAmPI, OWASP Top 10, PortSwigger labs |
| Detection Engineering / SOC | `resumes/detection.tex` | Custom NASL plugin authoring, MITRE ATT&CK, feed automation |
| Vulnerability Management | `resumes/vuln-mgmt.tex` | Nessus/OpenVAS/Qualys/NVD, triage→remediation ownership |

`profile-matcher` picks the sector via `role_profile` (title-weighted keyword match with
a win-margin gate; below the margin it falls back to `General Security` → the master).
Each file compiles via `tectonic` to exactly one page. Per-JD reframing (rewording
individual bullets further for a specific job, without changing facts/numbers/entities)
is `resume-tailor`'s job — see PLAN.md §5/§8 for build status.

---

## CLI tab-completion

`main.py` ships a zero-dependency bash completion script. Add to `~/.bashrc`:

```bash
source /home/tony/github/job-search/completions/main.py.bash
# optional convenience alias (also gets completion):
alias js='/home/tony/github/job-search/.venv/bin/python3 /home/tony/github/job-search/main.py'
complete -F _jobsearch_main_py js
```

Then `./main.py <TAB>` completes commands (bare **and** `--tag` forms), `./main.py apply
--<TAB>` completes that command's flags, and enum values (`--llm`, `--outcome`, `--source`)
complete too. Keep the script's lists in sync with the argparse subparsers.

## Automated applying

`main.py apply` prepares each ready job up to the **submit button** (résumé + cover letter +
answers + your personal facts) — you open the link and click Submit. *Fully*-automated,
hands-off submitting is intentionally **not** built (a human must review + submit — see
PLAN §6). The nearest thing is **browser-driving (Option 2)**: with the chrome-devtools MCP
configured and a logged-in browser profile (`PLAYWRIGHT_USER_DATA_DIR`), the orchestrator
fills the LinkedIn Easy-Apply form live, screenshots it, and **stops at the review gate** for
you to submit. That needs setup (MCP + login) not wired in this environment yet.

## Job Application Automation (Planned)

Future phase: an MCP server or Playwright CLI agent will:
1. Read a job description
2. Clone the master `.tex`, apply a role-specific transformation
3. Compile to PDF
4. Fill and submit the application form

This workspace is structured to make that easy — the master resume is the
single source of truth; all variants are derived from it.
