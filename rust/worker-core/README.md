# pg_worker_core

Engine-free core of the plangraph Rust worker. It runs one coding-agent attempt for a card and
decides nothing about what runs next. Temporal and keeper glue live in a separate crate.

| Module | Job |
|---|---|
| `config` | defaults: heartbeat 30 s, heartbeat timeout 120 s, start-to-close 90 min, stall 10 min, tail 50 lines, 3 attempts per leg tier + 1 promotion, wait cap 24 h |
| `worktree` | one git worktree per attempt from a base sha, branch `attempt/<card>/<n>` |
| `check` | run the card's check command with a timeout, keep the output tail |
| `confine` | positive write-set confinement and forbid paths over committed, uncommitted and untracked changes |
| `exit_class` | the exit classes and a classifier from observed signals |
| `budget` | pure retry / re-route / wait / promote / upgrade / quarantine / park policy |
| `stall` | child in its own process group; stall and timeout kill the whole group |
| `probe` | outcome probe before any retry: already done, partial, nothing |
| `janitor` | remove dead attempts after the probe; keep attempts whose check passes |
| `packet` | park packet `{card, tier, exit_class, check_tail}` |

Linux only (the worker runs in WSL2). `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test` in `rust/`.
