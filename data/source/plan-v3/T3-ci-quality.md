# T3 — CI and code quality

Owner: AutoOS L1-main. Rule (operator 2026-10-01): **GitHub CI runs on `main` only**; lanes prove themselves locally
(affected tests while working, full suite once before merge); external PRs still need the upstream workflows green on our
fork first (D-155).

| Lane | What | DONE (evidence) |
|---|---|---|
| T3-CIGREEN | Make main green (the only lane allowed to merge during T0-FREEZE): update stale expectations after TORDER/B2 (`test_sync_ide_models` ×2, `test_registry_render.py:1785`, servable tiers incl. `*-free-only-paid`, resolver "antigravity", spawner cooldown provider list, zed routing), wire `tests/test_credential_files_ignored.py` into a suite, install pytest in the CI shards for the prepush test, make `apply --dry-run` skip providers without a live leg | 3 consecutive green main runs; each fix names the commit that changed the expectation |
| T3-CIMAIN | Triggers: remove `pull_request` + `workflow_dispatch` from `ci.yml` (or guard `if: github.ref == 'refs/heads/main'`), add `branches: [main]` to `omnigraph-images.yml` push; a test pins the triggers | trigger test; no lane-branch runs for 48 h |
| T3-WIN | Windows job: re-enable on main (it is `if: false`) or move its coverage to the workstation's local-green record per merge; decide by cost measurement | Windows suite result attached to every merge touching .ps1/.psm1 |
| T3-PREPUSH | Local-green gate everywhere: pre-push hook in every lane worktree (fleet clones too), affected-tests mapping complete, CI-like git env, full suite required at `ready`, `quarantine/*` branches refused | tests (exist) green in CI; hook present in all worktrees |
| T3-PRINCIPLES | `coding-principles` skill gains MUST rules with sources: CI only on main; affected tests while working, full suite before merge; single source of truth; DRY; ≤ 400 lines per file; no PR while CI is red; generated files carry their generator | skill tests; rules referenced by the ready-check |
| T3-CLEAN | Delete or fold the 09-30 throwaway scripts (`tools/v-activate-*`, `diag-*`, `gw-*`, `dump-*`, `analyse-gemini-*`, `check-legs.py`, `matrix.py`, `probe-vertex-isolated.py`; `probe-vertex.py` only if OmniRoute's own health/probe covers it), the parallel gateway re-patch scripts (one patch-apply path), `infra/local-ai/litellm_config.yml` (superseded); archive `docs/handoff/*` older than 7 days into one dated index | each deletion: `git grep` shows no live reference; tests green; reviewers confirm |
| T3-SPLIT | 400-line rule: new/changed files must comply (CI check on changed files only); large legacy files get a split plan, executed when a lane touches them (start with `tools/autoos-agent.py` 8.9k lines, `tools/registry.py`, `configuration/litellm/config.yaml` → folder) | CI check active for changed files; split plan doc |
| T3-SCRUB | Merge ws-scrubgap (adds `tools/` to the public-scrub scan) after local CI; keep the username-in-history decision (accepted) | scan list includes tools/; scrub test green |

Order: T3-CIGREEN first (unblocks everything), then T3-CIMAIN + T3-PREPUSH, then T3-PRINCIPLES, T3-CLEAN, T3-SPLIT, T3-WIN.
