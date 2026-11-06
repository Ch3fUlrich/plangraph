# deploy/

Static templates for the hosts that run plangraph: systemd user units (`systemd/`) and an `nginx/`
example for the plan page location. Nothing here installs itself;
see `systemd/README.md` for the unit inventory and the gated install procedure.

## Live page: follow main and render

`plangraph-render.service` (user, `Type=oneshot`) plus `plangraph-render.timer` republish the static plan
page every 5 minutes. Three steps, in order:

1. `ExecStartPre` — `python3 -m plangraph.live_follow follow`: fast-forward the live tree
   (`$PLANGRAPH_LIVE_TREE`) to the head that is allowed to be published. Prefixed with `-`, so a follow
   failure still renders the page from whatever the tree already holds.
2. `ExecStart` — render `plangraph.keeper.cli render --cdn` into a temp file under `$PLANGRAPH_OUT`, then
   publish it with `cat "$T" > plan-cdn.html`. Two properties matter: the page is only published when the
   temp file is non-empty and contains `</html>`, so a failed render leaves the previous page served; and
   `cat >` overwrites in place, keeping the destination inode, because the web server bind-mounts that
   single file (a `mv` would swap the inode and the container would keep exposing the old one).
   The build stamp is not passed in: `render.py` falls back to `git rev-parse --short=10 HEAD` in the live
   tree, which is exactly the head step 1 fast-forwarded to.
3. `ExecStopPost` — `python3 -m plangraph.live_follow check`: compares the published page's stamp against
   the followed head. It is `ExecStopPost`, not `ExecStartPost`, because a `Type=oneshot` unit skips
   `ExecStartPost` when `ExecStart` fails and the staleness alert exists exactly for that case:
   `ExecStopPost` runs even when the render failed. It also runs with `-`, so it can never block the
   next tick.

**The gate.** A head is only fast-forwarded when its PR head sha has a green run of the pinned
workflow *file* (`PLANGRAPH_CI_WORKFLOW_PATH`, default `.github/workflows/tests.yml`) whose own
`head_sha` is that sha and whose `head_repository` is `PLANGRAPH_REPO` — a same-NAMED workflow in
another file, or a fork PR's copy of it, cannot decide the result; the run's `name` is not consulted.
A commit with no PR must carry its own green run. Docs-only PRs run only the scrub and the `ci-gate` job (the
workflow selects suites per change), so their heads are followed too. Keep `PLANGRAPH_STATE` outside
`PLANGRAPH_OUT`: the output directory is the web-served render directory, and the gate state does not
belong in it. The read-only token authorising
those GitHub queries lives only in the file named by `PLANGRAPH_GH_TOKEN_FILE` — a `KEY=VALUE` file
holding `GH_TOKEN`; keep it mode 0600 (live_follow does not check permissions) — and it is never printed
or copied into a unit or into `render.env`.

**Staleness alert.** If the published page lags the followed head for longer than `PLANGRAPH_ALERT_GRACE_S`
(default 1800 s, i.e. 30 min), `check` runs `PLANGRAPH_ALERT_CMD` with the alert line as its last argument
(argv, not stdin), once per green head until the page catches up (re-armed when it does).

**Install is a separate, gated deploy step** — this lane ships files only. On the render host: copy
`plangraph-render.service` and `plangraph-render.timer` to `~/.config/systemd/user`, put an edited
`render.env` at `~/.config/plangraph/render.env`, then `systemctl --user daemon-reload` and
`systemctl --user enable --now plangraph-render.timer`. Optional drop-ins live in
`plangraph-render.service.d/` as `.example` files (`kg.conf.example` for the knowledge-graph sidecar,
`live-tree.conf.example` for overriding the live tree location); copy without the suffix to make one live.

## Host Bootstrap & Spawner Setup

`bootstrap.sh` (Linux) and `bootstrap.ps1` (Windows) are the one plangraph-owned worker-online procedure.
Preconditions: install Python and plangraph, install required runtime and agent packages, sign in and set up trust for SSH yourself.
AutoOS is optional, only with `--autoos-root` / `-AutoOSRoot`; no discovery or environment dependency.
`--skip-autoos` / `-SkipAutoOS` remain compatible; `requirements-host.json` lists optional AutoOS components.

First a human runs the worker dry-run below and reviews per-rule counts from `stall_watchdog tick --dry-run`.
The existing no-findings status means count zero. Bootstrap dry-run only runs that read-only tick and prints
intended commands: no setup, catalog init, directories, config, units, tasks or systemctl changes.
Then a human or explicitly pre-authorized agent runs normal bootstrap: count, optional AutoOS, catalog init,
watchdog install, stopping on failure. Without authorization an agent stops after dry-run and reports the normal command.

Usage:
```powershell
# Windows PowerShell
.\deploy\bootstrap.ps1 -DryRun
.\deploy\bootstrap.ps1
```
```bash
# Linux
./deploy/bootstrap.sh --dry-run
./deploy/bootstrap.sh
```

Normal bootstrap calls `python3 -m plangraph.spawner.stall_watchdog install --host linux`, which creates user
units and enables `plangraph-stall-watchdog.timer`. Windows calls
`python -m plangraph.spawner.stall_watchdog install --host windows`: it creates the recurring **Plangraph Stall Watchdog** scheduled task, not a systemd timer.
These persistent steps require human action or explicit pre-authorization; no sudo or linger workaround.
On the orchestrator, separately run the same dry-run/review/authorized bootstrap to create units; the remote
timer stays disabled: enrollment never enables it. On the orchestrator, run `bootstrap.sh --probe-host NAME --probe-config FILE`
(PowerShell: `bootstrap.ps1 -ProbeHost NAME -ProbeConfig FILE`), first with `--dry-run` / `-DryRun`.
Probe enrollment requires POSIX (including `pwsh`); native Windows is rejected before config access because POSIX private modes are required.
Probe arguments run enrollment only, even with install flags: no unit creation or watchdog tick occurs.
FILE must be an existing private operator-managed `watchdog.env` with one `PLANGRAPH_PROBE_HOSTS` assignment.
Enrollment only adds the host once; it does not run worker setup or SSH. A human sets root/notify config, reviews it, then
explicitly runs `systemctl --user enable --now plangraph-remote-probe.timer` after approval.
See [remote probes](../docs/operations/spawner-remote-probes.md) for config semantics.
This supersedes HOST3's AutoOS requirement only for these steps, not hardware readiness, private CI or real-run evidence.
HOST-AGENT still owns sampling and its service or Run key, WSL2, GPU support, privacy, idempotence, drift, CPU budget;
this watchdog task is not that service. HOST-SAMPLES-API owns token ingest, server clock/staleness,
rollups/retention/migrations. Bootstrap depends on neither and adds no sampler.
For catalog/routing/MCP details see [`docs/SPAWNER.md`](../docs/SPAWNER.md).

### Default Omnigraph connection

After watchdog installation both bootstraps run the same fail-soft Omnigraph helper:
read site config, cache the client pinned only in `.mcp.json` with `npm cache add`,
install session environment, then verify `/healthz` using a bearer header.
Copy `site.env.example` outside the repository to `~/.config/plangraph/site.env`
(Windows: `%USERPROFILE%\.config\plangraph\site.env`), or select it with
`PLANGRAPH_SITE_ENV`. Supply `OMNIGRAPH_BASE_URL` and `OMNIGRAPH_TOKEN_FILE`;
the latter names an owner-only file containing one token line. Never commit either.
On Windows remove inherited grants to other accounts from that file's ACL.
Missing/private-file problems print `OPERATOR:` instructions without values.
Linux installs a mode-0600 profile, sourced once by `.profile` and existing
`.zshenv`; tokens are reread at shell start with permissions checked again.
Windows stores both values in the user environment. Start a new session
after setup (Windows applications may require sign-out to refresh environment).
Unchanged files and cached pins are skipped on rerun; verification still runs.
Dry-run only describes these steps: no reads of secrets, writes, npm or network.
No npm/curl diagnostics or private values are printed. A failed health check
reports only its status; an unconfirmed path is skipped without another request.
