Native Codex writer admission now refuses missing or non-executable cargo, git
or codex with `tool_missing` before creating a run. Run records retain the
resolved tool paths and effective PATH; the writer and final gates reuse that
PATH, including gate subprocesses. Host launch environments may supply Cargo
and nvm bin paths without relying on login shell initialization.

Regression coverage: Rust integration tests exercise each missing prerequisite,
non-executable tools, relative PATH resolution, and final gates invoking cargo
with the recorded writer PATH despite a different supervisor PATH.
