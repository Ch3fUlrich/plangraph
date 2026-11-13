# Fleet command ports

`rust/pg-fleet` provides native fleet commands with the legacy command names.
Build from `rust/` using `cargo build -p pg-fleet --release`. Install with
`cargo install --path pg-fleet` after reviewing the local cutover checklist.
This package does not change live PATHs or remove the source tools.

`handoff-check CARD [--log]` validates the eight-section, forty-line handoff
format and appends new Done entries to `FLEET_WORKLOG`, defaulting to
`~/.local/state/fleet/worklog.jsonl`. Appends take an exclusive file lock.
Secret-like filenames and configuration outside `liveness/` are refused.

`host-sync` implements the fleet PATH check for host setup. It checks
`handoff-check`, `resume-digest`, and `fleet-name`. Set `FLEET_BIN_DIR` to
the directory containing the selected installed binaries (or the existing
data checkout's router/bin during transition), and `FLEET_PATH_DIR` to an
existing PATH entry to repair missing commands. POSIX repairs create links;
Windows repairs copy executable files. A correct PATH makes no changes.
Every unresolved command prints `FAIL fleet PATH: COMMAND: REASON` and
causes exit 1. Invoke this fleet check from the host's synchronization flow;
it does not perform host-specific repository synchronization.

Parity fixtures record Python behavior before implementation; integration
tests use temporary homes, worklogs and repositories. No tests use live keeper
writes or live fleet sessions.

`resume-digest [--role ROLE]` and `resume-digest-carried [--role ROLE]`
retain the launch/card, snapshot, operator, local spawner/git, inbox, children
and transcript sources and legacy environment overrides. The digest reads one
complete local keeper snapshot and obtains the authenticated principal from
`kc --json whoami`. When that identity is available, claimed_by must match it;
an expired claimed lease cannot suppress LAPSED. If whoami is unavailable,
legacy role/owner matching remains available for offline use. The held list
retains expired rows with the EXPIRED diagnostic.

The original implementation compared claimed_by/owner with the display role,
never resolving whoami. That code explains how a different keeper principal
could omit MIMO-REASONING-ONLY-AFTER-781 and WIN-ATOMIC-WRITE-RENAME-EX and
produce false LAPSED lines. This is a code-based diagnosis: the historical
17:02 snapshot was not included in the brief. Synthetic recorded-shape cases
pin those two keys, both active states, another principal, and lease expiry.

`kc` forwards to the installed `pg` keeper CLI (`KC_PROGRAM` overrides the
executable). JSON output is complete; text over 3000 characters gets an explicit
truncation marker. Keeper configuration remains owned by that CLI.

The digest corpus contains 123 original Python executions with captured files,
timestamps and subprocess replies. Native tests replay them without Python or
network access on both platforms. The source suite passed 103 tests; its dense
page test's path-length assertion failed in the temporary reference checkout.
Its complete output is still pinned in the corpus, including the 120-line cap.

leet-name ROLE combines local and enabled remote session listings, retries one transport failure, and prefers a live session confirmed by the role's launch record during overlap. ROUTING_STATE_DIR and FLEET_HOST_POLICY retain their legacy meanings. Configure local aliases in the host policy when needed.
`question-count` reads QUESTIONS.md from the operational data directory and preserves the table row, kind, blocking and priority counting rules.
