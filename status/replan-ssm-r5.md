# SESSION-SPAWN-MCP replan after r4 (router rule: rework cycles > 2) · build-L1 gen-44 · 2026-10-30

## Why seats keep failing
- Each round fixes findings by ADDING code (sessions.py now 959 lines, 55 defs); the new edges are the next round's findings.
  r2 HIGH/r3 HIGH/r4 HIGH are all new failure paths in code added the round before.
- Scope crept past the brief: `wt` launcher, separate `style_file`, `file_roots`, per-file trust/symlink checks, context tail.
  These produced 4 of 6 themes below.
- No written threat model: seats grade against "any attacker", fixer answers piecemeal (stat->fd->O_NOFOLLOW->...), never closes.

## Findings r1-r4 by theme (H/M counts)
| theme | rounds | H | M | root cause |
|---|---|---|---|---|
| A file trust/symlink/TOCTOU (config, mcp_config, style_file, prompt/handoff) | r1-r4 | 2 | 7 | per-file ad-hoc checks; same-user attacker undefined |
| B launch/relaunch failure state (post-stop, post-launch, orphan, already-stopped, dup name, null id) | r2-r4 | 3 | 5 | record written AFTER launch; many partial-failure branches |
| C id resolution (short id, poll, exclude old id, cutoff) | r1-r3 | 0 | 4 | mostly fixed r3 |
| D audit completeness/identity/guarded write | r1-r3 | 0 | 5 | audit at many call sites |
| E MCP boundary args (required/types/unknown keys) | r1-r4 | 0 | 4 | hand checks, never from schema |
| F Windows (wt `;` injection, .cmd shim, bare claude, transcript path) | r1-r3 | 1 | 4 | `wt` launcher |
| G spec drift (defaults, 256 KB tail, docs) | r3 | 0 | 1 | - |

## Design changes (code, not just patches)
1. Threat model in docs/SPAWNER.md, stated once: caller = authenticated MCP principal; same-OS-account processes are trusted
   (they can already run `claude`); config must be a regular, non-symlink, 0600, owner-owned file (lstat + O_NOFOLLOW, refuse
   where O_NOFOLLOW is missing). Out of scope: same-account file swaps. Ends theme A.
2. Style text moves INTO the 0600 config (`style_text`), no `style_file`. Drop `file_roots` trust layer: prompt_file/handoff
   must be under an allowed cwd root, plain realpath containment. Ends A for style, H redaction of style.
3. Write-ahead record: record `{state: "launching", argv(redacted), launched_ms}` before the subprocess; then `running`+id, or
   `unknown` (id null, kept, listed, stoppable by name via `claude agents`). One terminal path; no post-launch orphan class (B).
4. Relaunch fail-closed: stop must be confirmed by `claude agents` (no row for the old id, agents call succeeded); any doubt
   -> refuse with old id + reason, keep marker. No `force` fallbacks (B).
5. One `_action` wrapper: validate -> act -> audit once (best effort, never changes outcome; principal from
   PLANGRAPH_PRINCIPAL or getuser) (D).
6. One boundary validator built from the declared tool schemas (required, JSON types, no extra keys), audited refusal (E).
7. Windows: drop `wt`. Same `claude --bg` path via resolved `claude.exe` (refuse .cmd/.bat), DETACHED_PROCESS; id from
   `backgrounded` + `claude agents --json` as Linux. Matches router note "truly detached start". Ends F.
8. Spec defaults as brief/fleet-launch: compact_window 330000, ctx_cap 300000; context = last 256 KB tail, "if known".

## Scope cut to the brief
Keep: launch/stop/relaunch/list/mark_ready, dry-run, name/cwd/model/effort/mode/mcp allowlists, audit, records in state dir.
Cut: wt launcher, style_file, file_roots, per-ancestor trust walk, force-relaunch.
Target: sessions.py <= ~550 lines; existing test names stay (tests for cut features move to refusal tests, no Test-Removal).

## Execution
- Take fixer r4 (run-5174c268) only if verify passes; else discard. Then ONE rewrite pass in <=400-line slices
  (mimo-pro, pinpoint briefs per change 1-8), full suite, then r5 seats FULL (not delta) luna inline + Sonnet + glm Go.
- Router: GO on this replan before the rewrite.
