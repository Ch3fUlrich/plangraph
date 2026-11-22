# Agent message timestamps

Install the Rust session guards with `tools/hooks/install_hooks.sh --session-rust`.
Register the installer's guarded `pg-hook message-time` command for
`PreToolUse` on `SendMessage` and the shell tool. It checks the message field
or a literal `fleet-msg send` invocation. The fleet client checks the resulting
message before it loads the transport, including when a shell expanded the
arguments. The fleet client uses the default installed binary automatically.
Set `PG_MESSAGE_TIME_HOOK` to an absolute executable path for a custom install.

The check reads UTC times (`HH:MMZ`, `HH:MM:SSZ`) on the current UTC date
and ISO timestamps with an explicit offset. It allows one minute of clock
tolerance. Approximate times use `about` before the timestamp, as in
`about 04:0xZ`. A refusal identifies the timestamp and exits 2.
