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
