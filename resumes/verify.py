#!/usr/bin/env python3
"""Verification gates for the master resume + the six sector bases.

Run from the repo root:  python3 resumes/verify.py

Gates (PLAN.md Phase 15 / SS9 2026-08-24):
  1. every file compiles to exactly one page
  2. every facts.json atom used by a file keeps its invariants verbatim
  3. no entity outside the ledger's allowlist for capabilities the owner has NOT claimed
  4. no seniority / tenure inflation

NOTE: PDF text must be de-hyphenated before scanning -- LaTeX breaks words across
lines ("Schnei-\nder Electric") and a naive substring scan reports false drops.
"""
import json, re, subprocess, sys

FILES = ["varakumar_resume.tex"] + [f"resumes/{n}.tex" for n in
         ["vapt", "appsec", "cloud", "detection", "ot-ics", "vuln-mgmt"]]

# token that signals an atom is used in a given file -> only then are its invariants required
ANCHORS = {
    "holm-cve-coverage": "3,200+", "holm-vendors": "Siemens", "holm-cloudsploit": "CloudSploit",
    "holm-ai-automation": "Claude AI", "billmgr-vulns": "Bill Manager",
    "billmgr-ghsa": "Security Advisory", "portswigger-labs": "PortSwigger",
    "ad-core": "Kerberoasting", "ad-advanced": "ADCS", "proj-enum-tool": "crt.sh",
    "proj-openvas": "NASL", "proj-lab-ad": "domain controller", "proj-lab-pfsense": "pfSense",
    "cert-oscp": "OSCP", "cert-tcm": "TCM Security", "edu-degree": "Malla Reddy",
}
# owner explicitly did NOT claim mobile app pentesting (2026-08-24)
FORBIDDEN = ["MobSF", "Frida", "Drozer", "JADX", "NetHunter", "mobile app"]
DENY = [r"\bled\b", r"\barchitected\b", r"\bsenior\b", r"\bmanaged a team\b", r"\bteam of\b",
        r"\bowned the\b", r"\b([3-9]|[1-9]\d)\+?\s*years\b"]


def rendered(tex):
    """PDF text, de-hyphenated and whitespace-collapsed."""
    out = subprocess.run(["pdftotext", "-layout", tex[:-4] + ".pdf", "-"],
                         capture_output=True, text=True).stdout
    out = re.sub(r"-\s*\n\s*", "", out)          # rejoin hyphenated line breaks
    return " ".join(out.split())


def main():
    atoms = {f["id"]: f for f in json.load(open("resumes/facts.json"))["facts"]}
    fails = 0

    print("== GATE 1: one page each ==")
    for t in FILES:
        info = subprocess.run(["pdfinfo", t[:-4] + ".pdf"], capture_output=True, text=True).stdout
        n = re.search(r"Pages:\s+(\d+)", info).group(1)
        ok = n == "1"; fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {t}: {n}")

    print("== GATE 2: fact invariants ==")
    g = 0
    for t in FILES:
        txt = rendered(t)
        for aid, tok in ANCHORS.items():
            if tok not in txt:
                continue
            miss = [i for i in atoms[aid]["invariants"] if i not in txt]
            if miss:
                g += 1; print(f"  FAIL {t} [{aid}] dropped: {miss}")
    fails += g
    print("  ok   all invariants preserved" if not g else f"  {g} failure(s)")

    print("== GATE 3: unclaimed-capability entities ==")
    g = 0
    for t in FILES:
        hits = [w for w in FORBIDDEN if w.lower() in rendered(t).lower()]
        if hits:
            g += 1; print(f"  FAIL {t}: {hits}")
    fails += g
    print("  ok   none present" if not g else "")

    print("== GATE 4: seniority / tenure inflation ==")
    g = 0
    for t in FILES:
        txt = rendered(t)
        for p in DENY:
            for m in re.finditer(p, txt, re.I):
                g += 1
                print(f"  HIT  {t} /{p}/ -> ...{txt[max(0, m.start()-55):m.end()+35]}...")
    # OSCP: its proper name contains "Certified Professional" -- legal ONLY while the
    # same line still says "In Progress" (facts.json cert-oscp claim_boundary).
    for t in FILES:
        txt = rendered(t)
        for m in re.finditer(r"Certified Professional.{0,60}", txt):
            if "In Progress" not in m.group(0):
                g += 1; print(f"  HIT  {t} OSCP claimed as earned -> ...{m.group(0)}...")
    fails += g
    print("  ok   clean" if not g else f"  {g} hit(s)")

    print(f"\n{'PASS' if not fails else 'FAIL'} - {fails} problem(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
