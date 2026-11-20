## LE-SESSION-NAME-COUNTER-NOT-ALLOCATED

- **bash_guard (D-158):** Unconditionally deny non-resume `claude --bg` /
  `--background` session launches in the Bash tool (including `--bg=0` /
  `--background=0` fail closed; `Sessions` launches `claude` via Python
  subprocess, never through Bash). Require a UUID or hex session id for
  `--resume` / `-r` and deny `--resume` combined with `-n`/`--name`, while
  keeping `claude -p`/`--print`, `claude --bg --resume <uuid|hex>`,
  `claude --resume`, `claude agents`, and `claude stop <id>` allowed; preserve
  all quoting and command-substitution denials.
