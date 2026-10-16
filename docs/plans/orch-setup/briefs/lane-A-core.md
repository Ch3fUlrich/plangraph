# Lane A: setup core (plangraph-L2-su-core)

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design sections: 4, 5.
Items, in order: `plangraph:SU-CORE`, then `plangraph:SU-ENV`. Starts once plangraph:SU-DOCS is merged.

## READ
- plangraph `rust/Cargo.toml` (workspace) and `rust/worker-core/src/config.rs` (existing config style).
- plangraph `src/plangraph/keeper/cli_verbs.py:78-109`, `src/plangraph/keeper/mcp.py:300-331`, `src/plangraph/keeper/config.py`:
  env names in use (PG_URL, PG_TOKEN_FILE, PG_CONFIG, ...).
- AutoOS `lib/linux/install.sh` `route_claude_to_gateway` (about line 2887): the reference read-merge-write pattern (SKIPPED,
  INVALID, backup only on change, fsync + replace).

## WRITE SCOPE
- `rust/plangraph-cli/**` (new crate, added to the workspace members), `setup/manifest.toml`, `setup/env.schema.toml`,
  `.env.example`.
- `src/plangraph/envfile.py` (new) plus the minimal call sites that load it: keeper CLI, MCP, tools that read PG_*.
- `tests/test_envfile.py`, `docs/SETUP.md` (new).

## SU-CORE spec
- Binary `plangraph`. Subcommand skeleton: `setup apply [--dry-run] | setup check | env check | --version`. Other subcommands
  are added by later lanes in their own modules: keep `main.rs` a thin dispatcher.
- `env` module: resolve the file (`PLANGRAPH_ENV_FILE` else `~/.config/plangraph/plangraph.env`), then parse KEY=VALUE lines
  (comments, quotes, no shell expansion). Precedence: process env > file > defaults from `setup/env.schema.toml`.
- `merge` module, JSON and TOML, the idempotency contract of design section 4:
  - refuse an unparsable target;
  - preserve unknown keys and key order;
  - write only owned entries;
  - backup `<file>.plangraph-backup-<UTC stamp>` 0600 only on change, keep the last 3;
  - temp + fsync + rename, with a lock file;
  - report lines `<component> <target> installed|updated|skipped|drift|refused <reason>`;
  - exit codes 0/1/2/3.
- `manifest` module: `setup/manifest.toml` is embedded with `include_str!`; schema doc in `docs/SETUP.md`.

Checks (tests in `rust/plangraph-cli/tests/`, fake HOME):
- unparsable target is refused and left unchanged;
- 2nd apply is all skipped;
- check reports drift after a manual edit;
- no backup when nothing changes;
- a concurrent apply waits on the lock.

## SU-ENV spec
- `setup/env.schema.toml` is the single list of keys (design section 5 table: name, default, description, `secret_path` flag).
  The Rust and Python loaders both read it; there is no second list.
- `plangraph env check`: unknown keys are reported. A key that is not `*_FILE` but holds a token-like value (length >= 20,
  high entropy) is refused.
- `envfile.py`: stdlib-only loader with the same precedence. The keeper CLI and MCP call it before reading `os.environ`.

Checks:
- precedence test in Rust and Python on the same fixture;
- the `.env.example` keys equal the schema keys (test);
- keeper suite green: `PYTHONPATH=src python -m pytest -q -n 8 tests` in WSL.

## Return
DONE per item: branch, head sha, check output tail, writer model(s), seat verdicts.
