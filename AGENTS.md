# plangraph: instructions for every agent

Every agent that works in this repository follows this file (CLAUDE.md imports it).
Skills live only in this repository, under `skills/`; the AutoOS repository holds none.

## Your role

Your brief or prompt names your role. Read the skill of that role before you act. When a
brief starts with a skill's text, that text is the skill: do not look for another copy.

- Batch writer (several items in one session, own review and fix loop):
  `skills/batch-writer/SKILL.md`
- Implementer of one closed brief: `skills/implement/SKILL.md`
- Reviewer: `skills/review/SKILL.md`
- Researcher: `skills/research/SKILL.md`
- Orchestrator: `skills/unattended-orchestration/SKILL.md` with its cards (batch mode:
  `cards/batch.md`) and `skills/phase-batch-orchestration/SKILL.md`
- Planner: `skills/plangraph-planning/SKILL.md`

## Every role

- Coding discipline: `skills/coding-principles/SKILL.md`.
- Language: new code is Rust with Rust tests. No new Python, shell or Node source file
  and no new line in `tools/new_code_allowlist.txt` without an operator D-id. Fixes
  inside existing Python files stay allowed until that file's port.
- One implementation per capability: extend what exists, add no second copy. Comments
  state behaviour; history goes in commits.
- Never push or merge unless your brief says so. Never change git config. Never bypass
  a hook. Never weaken or delete a test to make it pass.
- Never put a hostname, key, real user path or private project name into this repository.

## Other skills

- `skills/plangraph-spawn/SKILL.md`: dispatch per-host agents with plangraph cards.
- `skills/structured-memory/SKILL.md`: durable memory in the Omnigraph graph.
- `skills/mcp-servers-setup/SKILL.md`: the self-hosted MCP server stack.
- `skills/routing-registry/SKILL.md`: OmniRoute combos, providers, models and routes.
- `skills/homelab-access/SKILL.md`: reaching the hosts safely.
