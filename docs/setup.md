# Rust user setup

Build with `cd rust && cargo build -p plangraph-cli`. Install the resulting
`plangraph` binary on the user PATH. MCP transport and hook activation stay with their own
setup items; this core installs environment defaults and provides their merge engine.
`plangraph setup apply --dry-run` previews the embedded `setup/manifest.toml`;
`setup apply` applies it and `setup check` reports drift without writing.
The selected user home is HOME (USERPROFILE fallback on Windows).

Setup parses JSON before changing it, merges object keys and preserves unrelated
array entries and hook handlers. Superseded hook commands use anchored `*`
patterns and are replaced in place. Each manifest entry with superseded patterns
names exactly one desired hook command; separate entries preserve command identity. Unknown settings and matcher metadata remain.
Environment setup only adds missing defaults; the loader uses process environment,
then PLANGRAPH_ENV_FILE or `.config/plangraph/plangraph.env`, then built-in defaults.
Inline secret keys are refused by `env check`; token file paths are permitted.
No env values appear in diagnostics.

Every changed target has a same-directory lock, a private backup (last three kept),
and a synced temporary file atomically renamed over it. Symlinks and invalid
structures are refused. An unchanged apply skips without a backup. Check and
dry-run create neither directories nor locks. One action line reports each target.
Exit codes: 0 success, 1 check drift, 2 usage/refusal, 3 partial failure.
File content permissions are private on Unix; Windows uses the user's directory ACL.
The setup contract tests run on Linux and Windows CI.

Live service activation and private configuration belong to the operator cutover.

## Unit breaker

`plangraph unit-breaker --unit NAME [--threshold 5] -- COMMAND ARGS...` keeps
locked state under XDG_STATE_HOME/plangraph/breaker (LOCALAPPDATA on Windows).
It preserves Linux exit-code plus normalized SHA1 signatures from the reference;
Windows uses failure classes. Success resets, 130/143 are neutral, and five
identical failures persist a trip before disabling the unit and writing one alert.
PLANGRAPH_ALERT_INBOX selects a private alert file; otherwise stderr is used.
An unavailable manager leaves a `.tripped` marker. Corrupt state refuses execution.
`unit-breaker status` is the router status interface; `reset NAME` clears a trip.

`plangraph migrate-breaker --dry-run` previews the copy-once import from the old
state namespace. Apply without the flag; existing new state always wins and the
old state remains for rollback. `deploy/breaker` supplies the retained watchdog
wrapper and removal instructions for disabled watchers. Activating these templates
requires the operator's router decision; it is a cutover step, not setup activation.

## Taildrop

The keep decision and filename-only contract are in `docs/taildrop.md`.
The embedded manifest installs its Linux service/timer idempotently, pointing at
the current binary with systemd path quoting. Platform-qualified entries are
skipped on other systems. `plangraph taildrop-sort --dry-run` checks the intended
operation without fetching files or creating state; activation remains cutover.
