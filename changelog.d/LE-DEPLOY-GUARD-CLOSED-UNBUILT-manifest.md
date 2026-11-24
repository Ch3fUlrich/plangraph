Add `deploy/manifest.toml`, the one table of every Git-tracked systemd unit file under `deploy/` (any depth, service,
timer, socket, target, mount and path units and their `*.in` templates) and every workspace binary Cargo lists, with
host roles, env file names, first-run check, the installer of a template family and, for a binary, every
`install_paths` entry (path and roles) a consumer runs it from. The `plangraph-cli` `deploy_manifest` tests fail for a
unit file or binary target the manifest does not name, for an entry whose file is not tracked, for a program a unit
runs from a path or on a role its binary entry does not declare, and for an `EnvironmentFile` an entry omits;
`not_deployed = "<reason>"` is the only way to leave one out. `deploy/manifest.toml` and every unit suffix are
`RUST_INPUTS` entries in the CI selector. No deploy needed: nothing reads systemd or installs anything yet.
