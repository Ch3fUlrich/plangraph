# Lane G: release, AutoOS catalog, fresh check (plangraph-L2-su-release)

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design sections: 2, 6 (Release, AutoOS), 9.
Items, in order: `plangraph:SU-RELEASE`, `autoos:SU-CATALOG`, `plangraph:SU-FRESH-CHECK`.
- SU-RELEASE needs `plangraph:SPLIT-PUBLISH` (the public repo exists).
- SU-FRESH-CHECK needs every other setup item.
- workst-L1-autoos launches this lane only when those are done.

## READ
- plangraph `.github/workflows/*` (runner labels; per the re-scoped `plangraph:CI-RUNS-ON-WORKST` (S19) the private repos use the workstation runner, the public repo GitHub-hosted only).
- AutoOS `docs/catalog.md:34-77,212-223`, the `cost-gate` component in `catalog/windows.json` (about line 251) and
  `Install-AutoOSCostGate` (`AutoOS.Install.psm1` about line 914) as the script-provider example.
- AutoOS `AutoOS.Detect.psm1` `Get-AutoOSInstalledStatus` (about line 230) and `lib/linux/install.sh` `custom_is_installed`
  (about line 304).
- AutoOS AGENTS.md section 4 (second run = skipped).

## WRITE SCOPE
- plangraph: `.github/workflows/release.yml`, `docs/SETUP.md` (release section), `rust/plangraph-cli/tests/fresh_*.rs` or a
  script under `tools/setup/`.
- AutoOS: catalog entries in `catalog/windows.json` + `catalog/linux.json`, the install/detect functions, tests,
  `docs/catalog.md` row.

## SU-RELEASE spec
GitHub-hosted runners only (S18; the public repo never uses the workstation runner). A tag `vX.Y.Z` builds `plangraph-x86_64-unknown-linux-gnu` and `plangraph-x86_64-pc-windows-msvc.exe`, plus `SHA256SUMS`, and
attaches them to the GitHub release. Builds are reproducible from the tag (`--locked`). A test tag on the lane proves it.

## SU-CATALOG spec
- Component id `plangraph-setup`, the same id on both platforms. The catalog pins the tag + both sha256 values.
- Install: download, verify sha256 (mismatch refuses and removes the file), place it at `~/.local/bin/plangraph(.exe)`,
  then run `plangraph setup apply`.
- Detect = `plangraph setup check` exit 0. Second run = skipped. Dry-run supported. No site strings.

## SU-FRESH-CHECK spec
On a clean HOME (temp user dir) on WSL2 Linux and on Windows:
- `setup apply` twice, the 2nd all skipped;
- `setup check` 0 drift;
- `plangraph launch` a parent + child pair (`--bg`, a tiny prompt that ends with `DONE test`); the parent's inbox injects the
  DONE once;
- `whoami` ok through the MCP entry against a test keeper;
- a commit with a stray Plan-Item is fixed by the hook.

Record the outputs. Never on the operator's real profile.
