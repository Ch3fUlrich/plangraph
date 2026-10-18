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
  principals are exempt); merge locks are lease locks (not fencing) whose owner is stored as
  `<principal>:<owner>`, and the acquire response returns that prefixed owner, which release must
  send back verbatim.

## Pass criteria
Same asserts as locally, plus: no `unknown_shape` alarm, `answer_question` reports signal status
`sent`, the card outcome is `done` (detail `merged`), and the projector watermark is current.
