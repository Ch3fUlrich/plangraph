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

**The gate.** A head is only fast-forwarded when its PR head sha has a green `tests` workflow run on it;
a commit with no PR must carry its own green run. Docs-only PRs produce no run at all, so their heads are
not followed — the live page advances when the next code PR lands green. The read-only token authorising
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
