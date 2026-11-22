# Lesson Stop hook

Install the Rust session guards with `tools/hooks/install_hooks.sh --session-rust`.
Register the installer’s guarded `pg-hook lesson-stop` command for `Stop`,
including continuation Stop events. The hook reads the session transcript
provided by the harness and exits 2 while it finds unrecorded signals. The
diagnostic gives each signal's ID and kind. Transcript content stays local.

Record resolutions in `lesson-<session_id>.tsv` in
`PG_LESSON_LEDGER_DIR`. The default directory is
`$XDG_STATE_HOME/plangraph/lessons`, falling back to
`$HOME/.local/state/plangraph/lessons`. Each row has the signal ID, a tab and
either the new keeper LE key or `attached to <key>` for an existing item.
For example, `tool:tool1` can be paired with `plangraph:LE-COMMAND`.
An entry resolves only its own signal. Message IDs use transcript line and
content-block positions. Keep ledger files outside the repository.

The hook recognizes Claude and Codex tool failures and Claude teammate messages
with a leading BLOCKED, FAIL or CORRECTION status. Start operator corrections
with `Correction:` to record them as signals in either transcript format.
