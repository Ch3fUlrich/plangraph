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

## 2. Install (keeper host)

```sh
mkdir -p ~/.config/systemd/user
cp plangraph-projector-reconcile.service plangraph-projector-reconcile.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now plangraph-projector-reconcile.timer
loginctl enable-linger "$USER"
