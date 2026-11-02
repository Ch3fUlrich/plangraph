# plangraph Field Guide

Short, high-signal facts an L3 (implementer, reviewer, researcher) needs in this repo.
Curated by L2s: add a line when an L3 lost time to something not written here, and
drop a line that stopped being true. It is not yet injected into L3 briefs (planned);
when it is, measure rework (review rounds, reverted commits) before and after.
One fact per line, each with a pointer. Every backticked repo path (a token with a slash or a short
file extension, no placeholders) is checked by `tests/test_skills_roles.py`.

## Run and test

- Tests are plain pytest; `tests/conftest.py` puts `src/` on the import path, no install step.
- Run CI-pinned: `uv run --no-project --with-requirements requirements-ci.txt python -m pytest <files> -q`. Pins live in `requirements-ci.txt`.
- Run only what your change affects: `tools/affected_tests.py` (usage in `docs/TESTING.md`).
- The keeper (`src/plangraph/keeper/`) is POSIX-only (`fcntl`): its tests need Linux, macOS or WSL (`README.md`, `CONTRIBUTING.md`).
- Tests that need git set the identity per command via env (`GIT_AUTHOR_NAME` and friends) or `-c`, never global config: see `tests/test_ci_commit_gate.py`.
- Use synthetic fixtures in tests; the repo is public (`docs/PUBLIC.md`).

## Rules the gates enforce

- A tracked file stays at or under 400 lines: `tools/check_file_size.py`, rule in `CONTRIBUTING.md`; oversize legacy files are in `.filesize-baseline` and may only shrink.
- Every merged change adds a `CHANGELOG.md` entry under Unreleased (D-653): merged changes only, newest first.
- Every commit ends with a `Plan-Item: plangraph:<KEY>` trailer (`CONTRIBUTING.md`, "Commits").
- Public scrub: no hostname, IP, e-mail address, key or home path in a diff or message; the pre-push hook `tools/hooks/pre-push` runs `tools/split/scrub_check.py`.
- One lane is one branch `lane/<short-name>` and one small PR into `main` (`CONTRIBUTING.md`, "Lanes").

## Agents and orchestration

- Dispatch agents by card through the spawner, never by hand-picking a model: `skills/plangraph-spawn/SKILL.md`; details in `docs/SPAWNER.md`.
- Role skills: `skills/research/SKILL.md`, `skills/review/SKILL.md`, `skills/implement/SKILL.md`.
- Review policy (seats, distinct model families, rounds): `docs/REVIEW-METHOD.md`.
- Plan item and card format: `docs/PLAN-FORMAT.md`; planning workflow: `skills/plangraph-planning/SKILL.md`.

## Where things live

- Docs: `docs/` (keeper `docs/KEEPER.md`, runbook `docs/RUNBOOK.md`, decisions `docs/decisions/`).
- Source: `src/plangraph/`; tools and hooks: `tools/`; skills: `skills/`; CI: `.github/workflows/tests.yml`.
