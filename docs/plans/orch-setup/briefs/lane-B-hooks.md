# Lane B: fleet hooks in Rust (plangraph-L2-su-hooks)

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design sections: 3, 4 (hook merge rule), 6.
Items, in order: `plangraph:SU-HOOK-FLEET`, `plangraph:SU-HOOKS-WIRE`, `autoos:SU-ADR0003A`. Starts once SU-CORE is merged.

## READ (AutoOS at origin/main >= 6a553870)
- `tools/hooks/fleet_report.py`, `tools/hooks/fleet_inject.py`, `docs/fleet-status-hook.md`, `tests/test_fleet_hook.py`
  (15 cases). This is the behaviour to port 1:1.
- `docs/decisions/0003-no-claude-code-lifecycle-hooks.md`.
- plangraph `rust/plangraph-cli/` (lane A output: env, merge, manifest modules).

## WRITE SCOPE
- plangraph: `rust/plangraph-cli/src/hook/{mod,report,inject,inbox}.rs`, `rust/plangraph-cli/tests/hook_*.rs`,
  `rust/plangraph-cli/benches/` or a latency test binary, the `[hooks]` section of `setup/manifest.toml`,
  `rust/plangraph-cli/src/setup/hooks.rs` (merge into settings.json).
- AutoOS: `docs/decisions/0003a-*.md`, `docs/fleet-status-hook.md` (pointer + "reference only" note). No AutoOS code.

## SU-HOOK-FLEET spec
- FIRST, a spike (design 11.4): measure an empty Rust `main` as Claude Code would run it (absolute exe path, no shell
  wrapper): wall-clock, 200 runs, p95, Windows and WSL2. Report the floor to workst-L1-autoos. If Windows > 10 ms, the budget
  becomes floor + 5 ms by its ruling.
- `plangraph hook bench` prints the p95 of a no-op; `setup apply` wires the fleet hooks only when the budget holds, else
  reports `skipped budget <p95>`.
- `plangraph hook report` (Stop) and `plangraph hook inject` (PostToolUse, UserPromptSubmit): stdin JSON, silent failure
  (exit 0, no output).
- Same paths and env as the Python: `FLEET_SESSION_NAME`; the launch record scan in `FLEET_LAUNCH_DIR` matched by
  `session_id`; `FLEET_STATE_DIR`.
- Same behaviour: transcript tail of 256 KB; first status line; record `{ts, from, session_id, turn, status, line}`; the 300-char
  cap; redaction rules; `.seen` marker written only after a successful append; cursor advanced before printing; 20 rows / 4096
  chars plus `+k more`; untrusted framing text byte-identical.
- Symlink refusal: O_NOFOLLOW on Unix; on Windows open, then refuse a reparse point (FILE_ATTRIBUTE_REPARSE_POINT).
- Port all 15 cases with the same fixtures. Add a cross-implementation test: Python report then Rust inject, and Rust report then
  Python inject, on the same state dir (format compatibility).
- Latency: a no-op (no record, empty inbox) p95 < 10 ms over 200 runs on Windows and on WSL2 Linux. Put the measured numbers in
  the run record. The test fails above budget on the CI runner class named by workst-L1-autoos.

## SU-HOOKS-WIRE spec
- `plangraph setup apply` adds Stop -> `<abs path to plangraph> hook report` and PostToolUse + UserPromptSubmit ->
  `... hook inject` to `~/.claude/settings.json` under the design section 4 merge rule.
- `superseded` patterns in the manifest: `python3 <any>/tools/hooks/fleet_report.py` and `.../fleet_inject.py`, replaced in place.
- `setup check` reports drift.
- Fixture tests:
  - foreign hooks (incl. a SessionStart entry and a PreToolUse Bash guard) are byte-identical outside our entries;
  - 2nd run skipped;
  - backup only on change;
  - unparsable settings refused.

## SU-ADR0003A spec
ADR 0003a (status accepted, date): AutoOS still writes nothing to settings.json. The `plangraph-setup` component it runs does,
with the safeguards of the design section 4. It answers each ADR 0003 failure reason (no checkout path: an absolute binary path;
dedupe; backups only on change; no session registry). Public wording only.
