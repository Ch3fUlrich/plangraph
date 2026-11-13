# RT-P1 live run (steps for build-L1)

The local test `tests/e2e/test_rtp1_local_e2e.py` chains the whole RT-P1 flow on one machine
(dev server, keeper, Rust worker, projector, inbox answer + signal, review). This page lists what
changes to run the same chain against the cloud cluster once its certificates exist, and what only
a live run can prove. Status: **live steps, not yet run.**

## What the local test does (one pass, about 5 s)
1. Temporal dev server on loopback, its own ports and db file.
2. Real keeper API (SQLite, or Postgres with `PG_KEEPER_BACKEND=postgres`), item `demo:E2E-1`,
   principals: worker, router, system.
3. `pg-worker` with a PATH-stubbed `opencode`: first run asks `PG_QUESTION`, second run (the
   answer is in the prompt) writes the allowed file; a fixture git repo with a phase branch.
4. `Card` workflow started from Python; worker asks over HTTP; projector `run-once`
   (attempt 1 `needs_input`, item `needs_input`, zero alarms).
5. Router `POST /v1/answer_question`; the keeper signals through the Temporal HTTP API; attempt 2
   merges; projector again; a rebuild from history on a fresh keeper gives identical rows.
6. Review seat over the phase diff: pass, 429 on the anthropic leg then pass elsewhere, and fix.

## Run it against the cluster
The test starts its own dev server today. For the cluster, point the same steps at it (add an
`E2E_TEMPORAL_ADDRESS` switch to the fixture first; the rest of the flow is address-driven).

```
export TEMPORAL_CLI=<path to temporal binary>          # test is skipped without it
export PG_WORKER_BIN=<path to built pg-worker>         # cargo build --bin pg-worker
export PG_TEMPORAL_ADDR=https://<cluster-frontend>:7233 # worker; https needs the TLS vars below
export PG_NAMESPACE=<namespace>
export TEMPORAL_ADDRESS=<cluster-frontend>:7233        # projector + test client
export TEMPORAL_NAMESPACE=<namespace>
export TEMPORAL_TLS_CERT=<client.pem>  TEMPORAL_TLS_KEY=<client.key>  TEMPORAL_TLS_CA=<ca.pem>
export TEMPORAL_TLS_SERVER_NAME=<name on the server cert>
export PLANGRAPH_TEMPORAL_HTTP=https://<cluster-http-api>   # keeper side: answer signals
export PLANGRAPH_TEMPORAL_NAMESPACE=<namespace>
export PG_KEEPER_URL=https://<keeper-host>             # required: the worker refuses to start without it
export PG_KEEPER_TOKEN=<worker-role token>             # required with a URL; set-but-empty is a startup error
# PG_KEEPER_STUB=1 would allow an in-memory keeper, but only with PG_KEEPER_URL unset: never for a live run.
export PG_JAIL_BIN=<absolute path to built pg-jail>      # required: the worker fails closed without it (cargo build -p pg-jail); see docs/worker-sandbox.md
export PG_HARNESS_VERSIONS=opencode=<version>,claude=<version>  # pin; a drifted binary stops the worker
export PG_OPENCODE_SERVER_URL=<opencode server url>    # worker leg `opencode-server` (fallback family)
export PLANGRAPH_OPENCODE_SERVER_URL=<same url>        # review seat opencode leg adds --attach
export PG_CONFIG=<keeper.json>                         # projector opens the keeper db from this
# Never log tokens; router/system tokens stay on the keeper side.
export PG_BUILD_ID=<build id>  PG_DEPLOYMENT=pg-worker # versioning; set the current version first:
temporal worker deployment set-current-version --address "$TEMPORAL_ADDRESS" \
  --deployment-name pg-worker --build-id "$PG_BUILD_ID" --yes
python3 -m pytest -q -s tests/e2e/test_rtp1_local_e2e.py
```

The TLS variables are the ones `rust/worker/src/tls.rs` reads. The projector's `TemporalSource`
reads only `TEMPORAL_ADDRESS` and `TEMPORAL_NAMESPACE` and connects without TLS: it needs TLS
options before it can reach the cluster (live gap).

## What stays live-only
- The keeper behind TLS with real tokens (the local keeper is plain loopback).
- The worker on its WSL2 host, reaching the cluster and the keeper over the real network.
- Real `opencode` / Claude legs instead of the stub, including a real usage-limit reroute.
- Cluster behaviour: worker versioning on the managed service, namespace limits, history size.
- The keeper serves `/v1/attempts|park|complete|alarms|locks` (RT-P1-KEEPER-WORKER-VERBS); the
  local e2e and a live run use the same keeper doors. Remaining difference: `/v1/attempts` and
  `/v1/complete` only audit (attempt rows come from the projector; done belongs to the keeper's
  review path). `/v1/park` is bound to the card's claim (when a card carries one, the caller must be
  its holder or delegate with an unexpired lease; an optional body `epoch` must match; router/system
  principals are exempt); merge locks are lease locks, not store-side fencing, whose owner is stored as
  `<principal>:<owner>`, and the acquire response returns that prefixed owner, which release must
  send back verbatim.

## Pass criteria
Same asserts as locally, plus: no `unknown_shape` alarm, `answer_question` reports signal status
`sent`, the card outcome is `done` (detail `merged`), and the projector watermark is current.

## Pilot step A

Run `python3 tools/pilot_preflight.py preflight` on the pilot host before starting the pilot.
An unset or empty `PLANGRAPH_TEMPORAL_HTTP` is a refusal: the stub leg must not
merge until the keeper has this setting. All required worker settings above,
plus `PG_CARD_ALLOWLIST` (absolute existing UTF-8 file), must be present.
Token, allowlist and jail operands must be regular files, not symlinks; token
mode is checked on the same no-follow file descriptor used to read its bytes.
Use `python3 tools/pilot_preflight.py setup-repo /persistent/pilot-a` to create
an isolated main-branch repository with only `check.sh`, no remote, and a sibling
`pilot-a.cards.allow` containing exactly `pilot:PILOT-1` and `pilot:PILOT-2`.
Parent traversal (`..`) is refused. Git initialization is staged in a private
sibling directory so a failed init/add/commit leaves the destination retryable.
The allowlist requires 1–20 `pilot:PILOT-<digits>` keys, one per line; blank
lines and `#` comments are allowed. Preflight and verify enforce the same parser.
Set `PG_CARD_ALLOWLIST` to that sibling path; never use production cards.

Run `python3 tools/pilot_preflight.py verify --temporal-db /persistent/temporal.db
--jail-bin /absolute/pg-jail --repo /persistent/pilot-a` (one shell command).
If `PG_TOKEN_FILE` is set, it must name an absolute regular UTF-8 file with mode
0600 and a nonempty credential; otherwise `PG_KEEPER_TOKEN` must be nonempty.
Preflight accepts this same file-only credential mode. Binding the allowlist to
the documented repository sibling is operator-enforced: verify validates its
absolute path and pilot-key content, not its filename or repository association.
Persistence permits only ext2/3/4, xfs, btrfs, zfs and f2fs mounts, never `/tmp`.
Mount points are resolved before matching; at equal depth the last mountinfo
entry wins. Unresolvable mount points refuse verification. The `--jail-bin`
operand must exactly match `PG_JAIL_BIN`; a missing environment value refuses.
After a failed check, all later checks print `SKIP` and do not execute.
Non-auth children receive only PATH, LANG, HOME and TERM, with a private writable
HOME, timeouts and no credentials. Auth Git probes use the operator's real HOME
and preserve GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM, GIT_CONFIG_NOSYSTEM,
GIT_CONFIG_COUNT/KEY_*/VALUE_* and XDG_CONFIG_HOME to inspect active configuration.
Pins accept exact version output or `name version`; subprocess output is never
printed. `rust/pg-jail/src/main.rs:19–33` has no `--help` branch, so the jail
check asserts a regular executable only; it does not prove sandbox enforcement.
Record all command output and exit codes in the pilot host run evidence, along with the
isolated two-card run. Do not claim a live pass from hermetic tests: persistence,
auth, jail, pins and checks must each be OK on the pilot host. Live pilot host evidence is not
yet recorded by this step's implementation.
