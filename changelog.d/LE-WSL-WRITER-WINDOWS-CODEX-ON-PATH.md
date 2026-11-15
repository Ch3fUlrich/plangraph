### Fixed

- Native Codex writers resolve and pin the executable before launch and refuse
  Windows mount paths with `tool_wrong_platform`, including symlink targets.
  Document the Linux PATH export required by non-login WSL writer scripts.
