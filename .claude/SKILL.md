# Shared Skill Rules

These rules apply to **every** skill in `.claude/skills/`. Individual SKILL.md
files contain only what is unique to that skill (trigger description, procedure,
output format). Do not repeat these rules inside individual skill files.

## Models

- Default model for all skills: **sonnet** (latest Sonnet).

## Structure

- Each skill is a folder under `.claude/skills/` containing a `SKILL.md` and,
  when needed, a bundled `scripts/` folder.
- Skills are self-contained: everything a skill needs to run lives inside its
  folder (plus shared utilities in `execution/` when explicitly referenced).
- Push complexity into deterministic scripts; the SKILL.md handles intent and
  decision-making only.

## Frontmatter

Every SKILL.md starts with frontmatter containing at minimum `name`,
`description` (this drives auto-discovery — write it as trigger phrases), and
`model`.

## Execution conventions

- Run bundled scripts from the skill's own folder:
  `python3 .claude/skills/<skill>/scripts/<script>.py`
- Intermediate files (screenshots, downloads, scratch data) go in `.tmp/` and
  are never committed. Deliverables live in cloud services the user can access.
- Secrets come from `.env` / `credentials.json` / `token.json` — never hardcode
  them in scripts or SKILL.md files.

## Self-annealing rule

If a skill breaks and you fix it, **update the respective SKILL.md** (and this
file, if the rule is shared across skills) with the corrected rule or flow so
the failure cannot repeat. Broken → fixed → documented. The system gets
stronger after every failure. Do not create new skills without asking.
