Use a capped host-local sccache for spawner Cargo builds, pre-push preflight,
hook installation and CI. The opt-in setup component installs the checksummed
Linux package, and builds continue when sccache is unavailable.
