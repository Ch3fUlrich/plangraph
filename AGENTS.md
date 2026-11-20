# plangraph: instructions for every agent

Every agent that works in this repository follows this file (CLAUDE.md imports it).
Skills live only in this repository, under `skills/`; the AutoOS repository holds none.
This file is the index of the skills: every skill directory is named here with the moment
to read it, and there is no separate index skill.

## Your role

Your brief or prompt names your role. Read the skill of that role before you act. When a
brief starts with a skill's text, that text is the skill: do not look for another copy.

- Batch writer (one or more items in one session, own review and fix loop):
  `skills/batch-writer/SKILL.md`
- Implementer of one closed brief: `skills/implement/SKILL.md`
- Reviewer: `skills/review/SKILL.md`
- Researcher: `skills/research/SKILL.md`
- Orchestrator: `skills/unattended-orchestration/SKILL.md` with its cards (batch mode:
  `cards/batch.md`) and `skills/phase-batch-orchestration/SKILL.md`
- Planner: `skills/plangraph-planning/SKILL.md`

## Work modes

A writer gets its work in one of two modes. The first block of the brief says which, and
an orchestrator may run both side by side.

- Batch mode (the default): the brief starts with the batch-writer skill and lists one or
  more items. The writer implements them, has its head reviewed by another model family
  and fixes the findings, all in one session.
- Single-task mode (work mode `piecewise`): the brief starts with the implement skill and
  holds one closed task. The writer implements it and reports; its orchestrator starts
  the review and sends findings back as a new brief.

Reviewers and researchers work the same way in both modes. A brief that starts with no
skill text names the role in its first lines: follow that role's skill.

## Every role

- Do your brief and nothing else. If it is unclear, contradicts a rule in this file or
  needs a decision that is not yours, stop and say what is unclear. Do not guess.
- End with the report your role skill names: the files you changed, the verbatim summary
  line of every check you ran, and what you did not finish.
- Start another agent only where your role skill says so.
- Coding discipline: `skills/coding-principles/SKILL.md`.
- Language: new code is Rust with Rust tests. No new Python, shell or Node source file
  and no new line in `tools/new_code_allowlist.txt` without an operator D-id. Fixes
  inside existing Python files stay allowed until that file's port.
- One implementation per capability: extend what exists, add no second copy. Comments
  state behaviour; history goes in commits.
- Never push or merge unless your brief says so. Never change git config. Never bypass
  a hook. Never weaken or delete a test to make it pass.
- Never put a hostname, key, real user path or private project name into this repository.

## Other skills, and when to read them

- `skills/plangraph-spawn/SKILL.md`: before you dispatch an agent to a host with a card.
- `skills/structured-memory/SKILL.md`: at session start and end, to load and store
  decisions, rules and conventions in the Omnigraph graph.
- `skills/mcp-servers-setup/SKILL.md`: when an MCP server is missing or misbehaves.
- `skills/routing-registry/SKILL.md`: before you change a combo, provider, model or route.
- `skills/router-core/SKILL.md`: at router start and before any router step on more than one item
  (keeper plan batch, GO of a batch PR, grants for several lanes, grouped asks).

Instructions for reaching hosts are not a plangraph skill: they live in the repository
that owns those hosts.

## Where the rest is

- `docs/SPAWNER.md`: how the spawner routes, admits and binds verdicts.
- `docs/skills/rule-disposition.md`: every skill rule with the test or card that holds it.
- `changelog.d/`: one fragment per change.
