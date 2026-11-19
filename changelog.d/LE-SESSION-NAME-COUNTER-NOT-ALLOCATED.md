## LE-SESSION-NAME-COUNTER-NOT-ALLOCATED

- **bash_guard (D-158):** Deny manual `claude --bg` session launches outside the
  fleet launcher while keeping `claude -p`/`--print`, `claude --bg --resume <id>`,
  `claude --resume`, `claude agents`, `claude stop <id>`, and calls marked with
  `PLANGRAPH_FLEET_LAUNCH=1` allowed; preserve all quoting and command-substitution
  denials.
- **spawner sessions:** Set `PLANGRAPH_FLEET_LAUNCH=1` when `Sessions` launches or
  relaunches `claude --bg` (and report it in `dry_run` `env_keys`).
