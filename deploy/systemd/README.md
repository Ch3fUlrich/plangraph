# plangraph systemd units

Use `systemctl --user` for the user units below. The WSL cache guard uses
a root system service and timer.

## 1. Files

| File | Role | Host |
| --- | --- | --- |
| pg-snapshot-pull.service | reader-host local plan copy refresh every 5 min | reader host |
| pg-snapshot-pull.timer | timer driving pg-snapshot-pull.service every 5 min | reader host |
| pg-host-link.service + .timer | Tailscale link to the coordinator | worker host |
| pg-host-ssh-link@.service + @.timer | SSH link and outage evidence for each configured worker | coordinator |
| pg-ci-watch.service + .timer | Rust CI failure scan and keeper fix tasks every 5 min; see [CI watch](../../docs/ci-watch.md) | CI watcher host |
| pg-merge-close.service + .timer | `plangraph merge-close`: close every open, claimed or in-review keeper item a merge into main names by `Plan-Item`, every 10 min (the land step calls it too); needs `PG_URL` and a `PG_TOKEN_FILE` of role `system`; `plangraph merge-close --count` prints the merged-but-not-done count; see [Keeper](../../docs/KEEPER.md) | keeper host |
| plangraph-keeper.service | persistent keeper daemon | keeper host |
| pg-keeper-backup.service + .timer | daily keeper schema dump (newest 14 verified kept, ledger, restore test); see [keeper backup](../../docs/keeper-backup.md); needs `~/.config/plangraph/keeper-backup.env` (mode 0600, template `keeper-backup.env.example`) | keeper host |
| plangraph-projector.service | continuous projector run loop (30 s poll): attempts reach the keeper | keeper host |
| plangraph-projector-reconcile.service | one nightly run: reconcile open Temporal workflows vs keeper state | keeper host |
| plangraph-projector-reconcile.timer | nightly timer (03:30) starting the reconcile service | keeper host |
| plangraph-oversight-check.service + .timer | oversight check (digest files + projector_alarms) every 30 min | keeper host |
| plangraph-quota-alarms.service + .timer | quota 50/75/90 % alarms from the gateway quota_snapshots to routing every 15 min (read-only; require `QUOTA_SNAPSHOT_DB` in `~/.config/plangraph/quota-alarms.env` to a read-only mount or copy of the gateway sqlite so no docker group is needed; the service fails if this setting is missing; `systemctl --user enable --now plangraph-quota-alarms.timer` on the gateway host after copying both files) | gateway host |
| plangraph-review-probe.service + .timer | probe every routed review leg (`REVIEW_ORDER`) with a tiny OK prompt every 15 min (23 min maximum per run), booking down legs that fail and emitting a one-time `missing_manage_key` alert via `pg-spawnd check-manage-key` | spawner host |
| plangraph-render.service | one tick: follow the gated head, render the static page, check the stamp | render host |
| plangraph-render.timer | timer driving plangraph-render.service every 5 min | render host |
| pg-wsl-cache-guard.service + .timer | sample Windows free physical memory every three minutes and reclaim WSL page cache below the configured floor | WSL worker, system units |
| render.env.example | environment template for plangraph-render.service (paths, not secrets) | render host |
| plangraph-render.service.d/kg.conf.example | drop-in example: knowledge-graph sidecar endpoints | render host |
| plangraph-render.service.d/live-tree.conf.example | drop-in example: override the live tree location | render host |

## 2. Install (keeper host)

```sh
mkdir -p ~/.config/systemd/user
cp plangraph-projector-reconcile.service plangraph-projector-reconcile.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now plangraph-projector-reconcile.timer
loginctl enable-linger "$USER"
```

Linger makes the timer fire with no login session. A user timer cannot wake a suspended host; `Persistent=true` runs a missed
nightly pass at the next start.

## 3. Verify

```sh
systemctl --user list-timers plangraph-projector-reconcile.timer
systemctl --user start plangraph-projector-reconcile.service
journalctl --user -u plangraph-projector-reconcile.service -n 20
```

The last stdout line is one JSON object with `raised`, `cleared`, `open_alarms`, `counts`.

## 4. Paths

- Edit `WorkingDirectory`/`PYTHONPATH` if the checkout lives elsewhere.
- `keeper.json` is the same file the keeper daemon reads.
- Optional `projector.env` (`%h/.config/plangraph/projector.env`) for `TEMPORAL_ADDRESS`/`TEMPORAL_NAMESPACE`.

## 5. Alarms

A mismatch becomes an open row in `projector_alarms` (`cleared_at IS NULL`); the next pass clears it when the condition is gone. See `docs/projector.md` "Operations".

## 6. Deploy note

This lane ships files only; install on the keeper host is a separate, gated deploy step.

## 7. Projector run loop

Install:

```sh
mkdir -p ~/.config/systemd/user
cp plangraph-projector.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now plangraph-projector.service
```

Linger is covered in section 2.

Verify:

```sh
systemctl --user status plangraph-projector.service
journalctl --user -u plangraph-projector.service -n 20
```

Restart check:

```sh
systemctl --user kill -s KILL plangraph-projector.service
```

After `RestartSec` (5 s) status shows active again; `Restart=on-failure` restarts the loop on a signal exit.

Notes:

- Reads the same `projector.env` (`%h/.config/plangraph/projector.env`) as the reconcile service.
- Does not replace the nightly reconcile timer.
- Production placement stays with the release/deploy item.

### Worker SSH link

Install `pg-host-link` in `~/.local/bin`. Install both
`pg-host-ssh-link@.service` and `pg-host-ssh-link@.timer` in
`~/.config/systemd/user/`. Each instance reads
`~/.config/plangraph/host-links/<worker-id>.env` with
`PLANGRAPH_SPAWN_STATE`, `HOST_LINK_OBSERVER`, `HOST_LINK_SSH` (the exact SSH
destination callers use), and `HOST_LINK_PEER` (the Tailscale hostname, DNS
name, or address). Run `systemctl --user daemon-reload` and
`systemctl --user enable --now pg-host-ssh-link@<worker-id>.timer`.
Use `pg-host-link ssh-status STATE_ROOT DESTINATION` before an SSH step to
report a confirmed outage. The local journal logs the evidence for host L1.
The monitoring user needs read access to the `tailscaled.service` journal.

On workers, install `pg-host-link.service` and `pg-host-link.timer` and set
`PLANGRAPH_SPAWN_STATE`, `HOST_LINK_ID` and `HOST_LINK_PEER` (the coordinator
peer) in `~/.config/plangraph/host-link.env`. Run
`systemctl --user enable --now pg-host-link.timer` after daemon reload.

The existing stall-watchdog timer delivers the outbox. In its
`~/.config/plangraph/watchdog.env`, move the existing bridge argv to
`PLANGRAPH_WATCHDOG_FALLBACK` and set `PLANGRAPH_WATCHDOG_DELIVERY` to the JSON
argv for `plangraph host-link-deliver`, using the binary's absolute path.
Set `PG_URL`, `PG_TOKEN_FILE` (a principal allowed to post keeper messages),
and `PG_HOST_LINK_SENDER` (that principal's name). The keeper endpoint must be reachable independently of the link under
test. Successful delivery uses the watchdog event ID as the keeper message ID.

Read-only SSH waiters can use
`pg-host-link ssh-wait STATE_ROOT DESTINATION -- COMMAND [ARG...]`. It retries
SSH transport failures up to three checks and reports the confirmed outage
time when the monitor has one. Use `ssh-status` to guard steps with side effects.
`pg-package-host --state` uses the same state directory to queue launches when
the selected SSH route has a confirmed outage.


## Host release binaries

After fast-forwarding the main checkout, run `host-sync --install-release REPO`
with the host's shared `CARGO_TARGET_DIR` in the environment. On the first
rollout, build the entry point with
`cargo build --manifest-path REPO/rust/Cargo.toml --release -p plangraph-cli --bin host-sync`.
Use that entry point as the release step in the host-sync schedule. It installs
`pg-spawnd`, `pg-disk-hygiene` and `plangraph` in `~/.local/bin` and records the installed Rust
tree in `${XDG_STATE_HOME:-$HOME/.local/state}/plangraph/release-built-tree`.
Keep `~/.local/bin` on the landing and pre-push workers' PATH.


On disk-hygiene workers, run `host-sync --install-env disk-hygiene` to create
`~/.config/plangraph/disk-hygiene.env` with the checkout at `~/code/plangraph`,
its `.claude/worktrees` directory and `~/.config/plangraph/pins.json`. The
pins producer must publish that file before the first tick. Hosts also using
the worktree sweep should provision its environment with the sweep bootstrap
first, so the file includes the sweep settings.

On the WSL worker, an operator installs the trusted release artifact at
`/usr/local/bin/pg-spawnd` with `install -o root -g root -m 0755`, then runs
`host-sync --install-wsl-guard POLICY HOST` as root. The installer checks
that executable and writes the system units and the memory floor if the
floor file is absent. The policy's default floor is 20480 MiB. Include
retirement of the temporary WSL cache timer after replacement verification
and check for overlap with existing disk cleanup timers in the deploy ask.


On the keeper host, include `--with-keeper-backup` in the release install and
run `host-sync --install-env keeper-backup`. It creates the mode-0600 env file
from the empty connection fields in `keeper-backup.env.example`. The approved
key-placement session supplies the database credentials before the backup
unit is verified or enabled.


On the OmniRoute gateway host, run `host-sync --install-env quota-alarms`.
The installer uses `QUOTA_SNAPSHOT_DB` when supplied, then an explicitly
configured `OMNIROUTE_DATA_DIR`, then the host mount backing `/app/data`
when `QUOTA_GATEWAY_CONTAINER` is set. For an unmanaged local gateway it
looks in `~/.omniroute` and `${XDG_DATA_HOME:-$HOME/.local/share}/omniroute`.
It verifies that `storage.sqlite` can supply quota snapshots and reports the
searched paths on failure. The service opens the selected file read-only.
