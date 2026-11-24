Add `deploy/manifest.toml`, the one table of every systemd unit file under `deploy/` and every workspace
binary with its host roles, env file names, first-run check and, for a binary, every `install_paths` entry a
consumer runs it from. The `plangraph-cli` `deploy_manifest` tests fail for a unit file (service, timer,
socket, target, mount, path) or binary target the manifest does not name and for an entry whose file is gone;
`not_deployed = "<reason>"` is the only way to leave one out. The CI selector runs Rust CI for a change to
the manifest or a unit file. No deploy needed: nothing reads systemd or installs anything yet.
