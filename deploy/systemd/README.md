# plangraph systemd units (user units)

All units are user units (`systemctl --user`), not system units.

## 1. Files

| File | Role | Host |
| --- | --- | --- |
| pg-snapshot-pull.service | reader-host local plan copy refresh every 5 min | reader host |
| pg-snapshot-pull.timer | timer driving pg-snapshot-pull.service every 5 min | reader host |
| plangraph-keeper.service | persistent keeper daemon | keeper host |
| plangraph-projector-reconcile.service | one nightly run: reconcile open Temporal workflows vs keeper state | keeper host |
| plangraph-projector-reconcile.timer | nightly timer (03:30) starting the reconcile service | keeper host |
| plangraph-render.service | one tick: follow the gated head, render the static page, check the stamp | render host |
| plangraph-render.timer | timer driving plangraph-render.service every 5 min | render host |
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
