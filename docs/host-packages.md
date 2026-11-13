# Host package ownership

The Rust binary owns `plangraph install [--only core,agents|ID,...] --dry-run`,
`install --check`, and `install --yes`. Linux and WSL2 use Debian/Ubuntu apt.
Install the pinned plangraph release first; the Rust command needs no Python.
The existing bootstrap still requires Python for its catalog/watchdog steps.
Native Windows package installation is outside this item; PowerShell bootstrap
calls the installed Linux binary through `wsl.exe --exec` in the default distro.

| Component | Owned install source | Detection |
| --- | --- | --- |
| git | apt git | git --version |
| python | apt python3, pip, venv, setuptools, wheel | version plus required module availability |
| nodejs | NodeSource LTS repo script, then apt nodejs | Node >=20 and npm |
| tailscale | vendor HTTPS installer | tailscale version |
| opencode-cli | npm @opencode/cli | opencode --version |
| claude-code | npm @anthropic-ai/claude-code | claude --version |
| qwen-code | npm @qwen-code/qwen-code | qwen --version |
| gemini-cli | npm @google/gemini-cli | gemini --version |
| qoder-cli | vendor HTTPS installer | qodercli --version |

These identifiers and sources are ported from the read-only reference catalog.
`deploy/requirements-host.json` owns the core/agent groups; OmniRoute and agent
skill links are explicitly delegated to their other items. Unknown IDs refuse
before installation. npm components pull in Node first. npm installs to
`~/.local`, and detection includes `~/.local/bin`, so a second run skips without
editing a shell profile. Use that bin directory on the user's normal PATH.

Dry-run/check perform detection but never install or create installer state.
Real execution requires --yes and a per-user lock. apt uses root or existing
noninteractive sudo authorization; the command never obtains passwords or logs
credentials. Scripts download to private temporary files over HTTPS with bounded
time/size and HTTPS-only redirects, require a shebang, and execute as files.
No shell pipeline executes a network stream. NodeSource is privileged repo setup;
Qoder's installer runs as the current user. Tailscale authentication remains
operator-only. Installation verifies each binary before proceeding and stops on
failure. Configuration/platform refusals exit 2; execution or verification failures
exit 3 because a package manager may already have changed the system. Check
drift exits 1; successful or skipped installation exits 0. Setup does not launch agents or perform paid probes.

Local execution evidence uses disposable command fixtures: all nine components
install through the real plan/executor interface, then skip on repeat; the real
CLI's NodeSource path is exercised against fake apt/curl binaries. This is not a
claim of successful fresh-host package-manager/network installation. A fresh
Linux host and a real WSL2 distro must still record dry-run, actual install and
repeat-run evidence at operator cutover. Windows runtime tests also await CI.

AutoOS remains read-only in this package. Its parallel removal owner should
replace the relevant legacy setup entry with a thin caller of
`plangraph install --only core,agents --yes`, forwarding dry-run. No code in
plangraph invokes AutoOS setup, and no AutoOS source was modified.

Vendor scripts are unpinned and refuse before downloading or preparing packages
unless the operator explicitly passes `--allow-unpinned-scripts`. `--yes` alone
does not permit them; dry-run/check remain available without that opt-in.
