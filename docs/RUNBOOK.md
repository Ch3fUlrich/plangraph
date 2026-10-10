# Keeper runbook

One keeper = one SQLite DB, one writer, HTTP on loopback, YAML import/export.
`pg` below is `python -m plangraph.keeper.cli` (`daemon`/`mcp` are modules).
Keep all state in one directory owned by the keeper's unix user:

    export KEEPER=$HOME/keeper
    mkdir -p "$KEEPER" && chmod 700 "$KEEPER"
    pg() { python -m plangraph.keeper.cli "$@"; }
## 1. Init and config
Write `$KEEPER/keeper.json`; any mode other than **600** is refused:

    {"db": "/home/me/keeper/keeper.db",
     "snapshot": "/home/me/keeper/plan.snapshot.json",
     "bind": "127.0.0.1:8765",
     "reap_interval": 60}

    chmod 600 "$KEEPER/keeper.json"
    export PG_CONFIG="$KEEPER/keeper.json"
    pg admin init --router router          # prints the router token ONCE

A full config, with the verifier paths and the legacy/decision-close settings
(X7b) filled in:

    {"db": "/home/me/keeper/keeper.db",
     "snapshot": "/home/me/keeper/plan.snapshot.json",
     "bind": "127.0.0.1:8765",
     "reap_interval": 60,
     "repos": {"server": "/home/s/code/Server", "autoos": "/home/s/code/AutoOS"},
     "decisions_file": "/home/s/code/routing/docs/decisions-log.md",
     "batch_roots": ["/home/s/code/routing/docs/plan"]}

`batch_roots` is the **only** place `close_on_decision` may read a batch file from.
Empty (the default) means the verb always refuses, so leave it unset until the
operator has decided which plan/batch directory is authoritative.  Relative
entries resolve next to the config file; containment is checked on the resolved
real path, so a `..` or a symlink that escapes the list is refused.
## 2. Principals
    pg admin add-principal l1       --role l1       --projects autoos,routing --family teamA
    pg admin add-principal worker   --role worker   --projects autoos         --family teamB
    pg admin add-principal reviewer --role reviewer --family teamC

Roles: router, l1, worker, reviewer, reader, system (system is internal).
Each token is shown once: save it to `$KEEPER/tokens/<name>.token`, mode 600.
A reviewer whose family equals the claimant's or delegate's is refused.
## 3. Run
    python -m plangraph.keeper.daemon --config "$PG_CONFIG"   # SIGTERM/SIGINT stops it
    export PG_URL=http://127.0.0.1:8765
    export PG_TOKEN_FILE="$KEEPER/tokens/router.token"
    pg ready --brief            # one compact line per ready item
    pg show KEY
    pg claim KEY --lease 2h
    pg verdict KEY --pass
    PG_URL=$PG_URL PG_TOKEN_FILE=$PG_TOKEN_FILE python -m plangraph.keeper.mcp

`pg` exit codes: 0 ok, 2 refused, 3 unreachable.  With the daemon down a read
(`pg ready`) answers from `plan.snapshot.json` read-only and says so on stderr;
a write still exits 3.  MCP is stdio JSON-RPC (`tools/list`, `tools/call`) and
never logs the token.
## 4. Import / export / verify / back up
    pg admin import DIR --as router   # YAML dir -> import_items (open/claimed/in_review)
    pg admin export DIR               # non-terminal items -> one YAML file per track
    pg admin verify                   # hash chain + cache integrity; exit 0 = ok

The daemon rewrites `plan.snapshot.json` after every committed write.  Back up
with SQLite's own online backup API (safe while running; the `sqlite3` CLI is not
installed, so use Python's) plus the snapshot:

    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$KEEPER/keeper.db" "$KEEPER/backup.db"
    cp "$KEEPER/plan.snapshot.json" "$KEEPER/backup.snapshot.json"

## 5. Legacy work and decision closes (X7b)
Two router-only paths finish work that never sees a reviewer.  Both are for
**legacy / operator-decided** items; the normal `standard`/`high` review path is
unchanged, and both fail closed when the config is not filled in.

Attach the historical proof, then close the batch (router principal only). The
same verbs are on every door (`Store`, HTTP `POST /v1/...`, MCP tool, `pg`):

    store.add_evidence_router(router, "routing:R-117", "commit",
                              {"commit": "<40-hex sha>"})
    store.close_on_decision(router, "routing:R-117",
                            "/home/s/code/routing/docs/plan/batch-7.md@" + "<sha256>")

* `commit` passes on "the sha exists and is an ancestor of `main`" in that
  project's `repos` path — no trailer, no date window, no CI.  It is the weakest
  proof the keeper accepts and **no other principal may attach it** (a claimant
  trying gets `forbidden`).
* `close_on_decision` needs the item at `review_tier='none'` (decision-only
  definition, settable by the router alone) in `claimed`/`in_review`, at least
  one attached `decision` evidence, every one verifiable in
  `decisions_file`, and a batch file inside `batch_roots` whose bytes hash to
  the digest in the ref.  If the decision row names anything
  (`Closes: project:ID`, whole tokens, like `Waives:`), this key must be one of
  them; with no `Closes:` token the hashed batch file alone authorises it.
* The resulting `closed_on_decision` event locks the item exactly like
  `review_passed`: afterwards edit, cancel and further evidence are refused
  (`locked`), and `pg admin verify` (fold + chain) agrees with the cache.
* Nothing here closes an `open` legacy row: move it into `claimed`/`in_review`
  first with `set_legacy_state` (router only; any other target is `validation`,
  a non-open item is `bad_state`).  `claimed_by` becomes the item's owner and the
  `legacy_state_set` event keeps fold + `pg admin verify` agreeing.

### Doors for `set_legacy_state` and `close_on_decision` (X7c)

    pg define --file batch.json --tier none          # decision-only definition
    pg legacy routing:R-117 claimed "backfilled"     # open -> claimed/in_review
    pg close  routing:R-117 --batch /abs/plan/batch-7.md@<sha256>

Over HTTP: `POST /v1/set_legacy_state {"key","state","note"}` and
`POST /v1/close_on_decision {"key","batch_ref"}`; over MCP the tools
`set_legacy_state` and `close_on_decision`.  A non-router token on either is
`403 forbidden`, exactly like `waive`.  `close_on_decision` is refused `forbidden`
until `batch_roots` is set in the config.

## 6. Isolation — read this
The keeper separates agents by OS user: each token is a mode-600 file owned by
that user, and the DB is a local file.  **Isolation is NOT effective if agents
run as the same unix user, or a user is in the `docker`/`lxd` group (host root),
or anything else can read the DB or a token file** — such an agent can act as
another principal.  Use one unix user per agent and one DB host; otherwise treat
the keeper as an audit trail, not a sandbox.

## 7. Markdown import (`tools/md_sync.py`, SYNC-MD-IMPORT v1)
One-way, idempotent: the router's `QUESTIONS.md` becomes open lanes (`routing:<ID>`,
title prefix `Q:` / `OP:`, reachable through the gate `routing:PH-OPERATOR`), and
`ANSWERS.md` entries `- <ID> <words>: <text> (D-n[, D-m])` close the item on those
decisions (`edit` tier none -> `legacy_state in_review` -> `add_evidence decision` ->
`close_on_decision`, `batch_ref` = the ANSWERS.md path at its sha256).  An id that already
exists in any project is reused, never duplicated.  `decisions-log.md` is checked: an entry
citing an unknown D-id is refused and reported, an entry without a D-id is
skipped and reported, items gone from QUESTIONS are reported, never cancelled.  Every cited
D-id is also pre-checked with the keeper's own parser (`DecisionsAdapter`): if that decision's
`Closes:` list does not contain the item's key, the item gets **no ops at all** (no `edit`,
`legacy_state`, `add_evidence` or `close_on_decision` — the keeper would refuse and the atomic
batch would roll back), and the run says `close-skipped <ID>: <D-n> Closes does not list <key>`
plus `close-skipped <n>` at the end of the `md-sync:` summary.  Token-like
strings are masked `[redacted]` before any text reaches the keeper (count in the report).

**Archive switch (PG-RF-ARCHIVE-ANSWERS, default OFF).** Answers live in the keeper (Q9), so QUESTIONS.md can be
generated instead of imported: `python3 tools/md_sync.py --archive-answers --questions-out PATH --url U --token-file F
[--project routing] [--apply] [--status-file FILE]` skips the import (no QUESTIONS/ANSWERS/decisions inputs, no ops) and writes
the project's open operator items (`views.needs_you`: question / decision / operator_task, in that order, `choices` as an
`Options:` block) as the priority table above, atomically (tmp + rename). Without `--apply` it only reports (`dry run`). The file
round-trips through the importer's parser; cells are single-line and credential-masked. Status file: `{ts, mode: "archive",
archived, written, error}`. Turning the switch on in the timer unit is a later live step.

    python3 tools/md_sync.py --questions /home/s/code/routing/QUESTIONS.md \
        --answers /home/s/code/routing/ANSWERS.md \
        --decisions /home/s/code/routing/docs/decisions-log.md \
        --url http://127.0.0.1:8765 --token-file ~/keeper/router.token \
        [--dry-run | --apply] [--status-file /path/md-sync.status.json]

`--dry-run` (default) reads the live graph and prints the plan and counts; `--apply` posts
ONE atomic `apply_ops` call (all or nothing; a second run emits zero ops).  Exit codes as
`apply_batch.py`: 0 ok, 1 refused (also when an ANSWERS entry was refused), 2 usage/token/IO.
`close_on_decision` needs the router checkout in the keeper config `batch_roots`
(`/home/s/code/routing`).  The principal in the batch is `--principal` (default `router`)
and must match the token's name.

`--status-file PATH` records the run for the router as one JSON object, written atomically
(temp file + rename) on **every** run: dry or apply, success, refusal or early error.  The
keys, in this order: `ts` (ISO-8601 UTC), `mode` (`dry`/`apply`), `ops`, `applied` (the batch
really landed), `skipped`, `refused`, `gone`, `redactions`, `close_skipped` (a list of
`{"id", "decision", "key"}`, so the router sees skipped closes without parsing the report) and
`error` (one line, or `null`).  Ids and counts only — no titles, decision texts, descriptions
or tokens ever reach the file.  Without the flag nothing is written.

Run it every 10 minutes and on change (`systemd --user`; `~/.config/systemd/user/`), with a
directory for the status file:

    mkdir -p %h/.local/share/plangraph

    # md-sync.service
    [Unit]
    Description=plangraph markdown import
    [Service]
    Type=oneshot
    ExecStart=/usr/bin/python3 %h/code/plangraph/tools/md_sync.py \
      --questions %h/code/routing/QUESTIONS.md --answers %h/code/routing/ANSWERS.md \
      --decisions %h/code/routing/docs/decisions-log.md \
      --url http://127.0.0.1:8765 --token-file %h/keeper/router.token --apply \
      --status-file %h/.local/share/plangraph/md-sync.status.json

    # md-sync.timer
    [Unit]
    Description=plangraph markdown import every 10 minutes
    [Timer]
    OnBootSec=2min
    OnUnitActiveSec=10min
    [Install]
    WantedBy=timers.target

    # md-sync.path  (also run on change; starts md-sync.service)
    [Unit]
    Description=plangraph markdown import on change
    [Path]
    PathChanged=%h/code/routing/QUESTIONS.md
    PathChanged=%h/code/routing/ANSWERS.md
    PathChanged=%h/code/routing/docs/decisions-log.md
    [Install]
    WantedBy=default.target

    systemctl --user daemon-reload && systemctl --user enable --now md-sync.timer md-sync.path
