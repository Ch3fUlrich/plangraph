# Fleet command ports

`rust/pg-fleet` supplies native fleet commands under the legacy names. Build
from `rust/` with `cargo build -p pg-fleet --release`. Install the selected runtime
binaries into a private command directory after reviewing the local cutover
checklist. Keep the fleet-only host-sync helper outside the normal PATH so it
does not shadow an existing host synchronization command. This package leaves
live PATHs and the source tools untouched.

`handoff-check CARD [--log]` validates the eight-section, forty-line card format.
It appends new Done entries to FLEET_WORKLOG, defaulting to
`~/.local/state/fleet/worklog.jsonl`, under an exclusive file lock. Secret-like
filenames and configuration outside `liveness/` are refused.

`fleet-name ROLE` combines local and enabled remote listings, retries one
transport failure, and prefers the live session confirmed by its launch record
during replacement overlap. ROUTING_STATE_DIR and FLEET_HOST_POLICY retain their
meanings. Host records can declare local aliases in `aliases`; deployment-specific
names stay in the private policy.

`question-count` reads QUESTIONS.md in the operational data directory and retains
the table row, kind, blocking and priority counting rules.

`resume-digest [--role ROLE]` retains launch/card, snapshot, operator, local
spawner/git, inbox, children and transcript sources and their environment
overrides. The carried-message implementation is `pg_fleet::carried`; the
legacy import-only resume-digest-carried file's executable remains a quiet no-op.

The digest reads one complete local keeper snapshot and obtains the authenticated
principal with `kc --json whoami`. With that identity available, claimed_by must
match it; an expired lease cannot suppress LAPSED. Offline operation
retains legacy role/owner matching when whoami is unavailable. The held list
keeps expired rows with an EXPIRED diagnostic.

The original code compared claimed_by/owner with the display role without
resolving whoami. That explains how a different keeper principal could omit
MIMO-REASONING-ONLY-AFTER-781 and WIN-ATOMIC-WRITE-RENAME-EX and produce false
LAPSED lines. This is a code-based diagnosis: the historical 17:02 snapshot was
not supplied. Synthetic recorded-shape regressions pin both keys and active
states, another principal, and lease expiry.

`kc` forwards to the installed `pg` keeper CLI; KC_PROGRAM overrides its
executable. JSON is complete. Text over 3000 characters has an explicit
truncation marker. Keeper configuration remains owned by that CLI.

`go-row-gate` retains GO-HEAD binding, the PLAN receipt grammar, keeper checks
and session restrictions. Without --log it fetches origin/main and reads the
decision log from that ref. Wrapper resolution skips empty and relative PATH
entries. Explicit PG_PGK and PG_CI_EVIDENCE overrides remain supported.

`merge-part [--no-claim] PR HEAD_PREFIX ITEM_KEY GO_DID SUBJECT BODY_FILE` retains
the ordered head, gate, keeper, mergeability and body checks, stale-claim retry,
five pinned REST attempts and evidence handoff. Its wrapper resolver is also
available as `merge_paths pgk|evidence`.

`inreview-backfill` retains TSV reports, project filtering, newest first-parent
merge binding, historical GO forms and missing-row reports. Apply requires
--apply, --go and --rows together. Dry runs print commands without posting.
Seat lookup, writer-family/token-directory requirements, verifier arguments,
refusal text and counts retain the source contract. PG_FLEET_PYTHON can select
the existing seat-verdict interpreter.

`work-summary [--since 24h|<n>h|<n>d|<ISO>] [--project ID]` combines worklog,
newest cards, keeper exports and first-parent main commits, with source precedence
and item/ref deduplication. It retains the forty-line output, project ordering,
list counts and stale/missing-source notes. WORK_SUMMARY_HOME and
WORK_SUMMARY_KEEPER_EXPORT select its operational data.

The fleet-only `host-sync` helper checks handoff-check, resume-digest and
fleet-name. FLEET_BIN_DIR selects installed binaries or the legacy router/bin
during transition; FLEET_PATH_DIR selects a repair destination already on PATH.
POSIX repairs create links; Windows repairs copy executables. Correct PATHs
make no changes. Every unresolved command prints
`FAIL fleet PATH: COMMAND: REASON` and causes exit 1. Invoke this helper by
absolute path from the host's existing synchronization flow.

Parity data records Python behavior before implementation: fifty card grammar
cases, 123 digest executions, 220 GO gate executions, 146 historical approval
inputs, 147 report/missing-report pairs and 33 summary executions. Runtime
replay uses Rust, temporary files and recorded subprocess replies. Native tests
also cover logging, wrapper resolution, authorization, refusals, retries and
real local git histories. They make no live keeper writes or GitHub merges.
Git fixtures reuse the workspace sandbox helper, clear inherited GIT_* state,
and supply temporary HOME and GIT_CONFIG_GLOBAL paths.

The reference digest suite passed 103 tests; its dense page test's path-length
assertion failed in the temporary reference checkout. That execution's complete
output remains pinned, including the 120-line cap. Other recorded reference
suites passed. Deployment skill prose and the private project registry are
represented by synthetic schema cases rather than copied into public fixtures.

From `rust/`, run `cargo test -p pg-fleet` on Windows and Linux/WSL, then
`cargo fmt --check` and
`cargo clippy -p pg-fleet --all-targets --no-deps -- -D warnings`.
The last command limits lint enforcement to this crate; existing dependency
warnings remain outside this package. No source or test file exceeds 400 lines.
