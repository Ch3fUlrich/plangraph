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
with SQLite's own copy (safe while running; WAL is checkpointed) plus the snapshot:

    sqlite3 "$KEEPER/keeper.db" ".backup '$KEEPER/backup.db'"
    cp "$KEEPER/plan.snapshot.json" "$KEEPER/backup.snapshot.json"

## 5. Isolation — read this
The keeper separates agents by OS user: each token is a mode-600 file owned by
that user, and the DB is a local file.  **Isolation is NOT effective if agents
run as the same unix user, or a user is in the `docker`/`lxd` group (host root),
or anything else can read the DB or a token file** — such an agent can act as
another principal.  Use one unix user per agent and one DB host; otherwise treat
the keeper as an audit trail, not a sandbox.
