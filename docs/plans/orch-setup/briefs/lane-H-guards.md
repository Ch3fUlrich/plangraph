# Lane H: guard hooks in Rust (plangraph-L2-su-guards), launched later

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design sections: 3 (hook inventory), 6.
Items: `plangraph:SU-HOOK-BASHGUARD`, `plangraph:SU-HOOK-CTXGUARD` (the two in parallel), then `autoos:SU-GUARDS-CALLER`.
Starts after `plangraph:PH-ORCH-SETUP`. workst-L1-autoos refreshes this brief at launch.

## READ (AutoOS at origin/main)
- `tools/hooks/bash_guard.py` (about 675 lines), `tests/test_bash_guard.py`, `docs/ai/fleet-hooks.md:37-63`.
- `tools/hooks/context_guard.py` (about 314 lines), `tests/test_context_guard.py`, and
  `.agents/skills/unattended-orchestration/references/main-orchestrator.md` around line 220 (wiring, thresholds
  `AUTOOS_CTX_HANDOFF` / `AUTOOS_CTX_HARD`, subcommands `ready`, `status`).
- `configuration/opencode/plugins/bash-guard/index.mjs` around line 1547 (the plugin's call).
- Lane B's `hook` module (shared stdin/exit handling).

## WRITE SCOPE
- plangraph: `rust/plangraph-cli/src/hook/{bash_guard,context_guard}.rs` + tests + manifest entries.
- AutoOS: the plugin call site + its test.

## Spec
- 1:1 behaviour, all Python cases ported, fail-open where the Python fails open.
- Exit 2 deny semantics as in bash_guard; state dir of context_guard unchanged (`$XDG_STATE_HOME/autoos/ctx-guard/`, so running
  sessions keep their counts).
- No-op p95 < 10 ms (numbers in the record).
- Setup wires them with matchers `Bash` and `*`; superseded patterns cover the Python commands.
- The plugin calls `plangraph hook bash-guard` and falls back to python only when the binary is absent.
