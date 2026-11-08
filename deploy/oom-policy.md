# OOM policy (D-5075)

Templates only: nothing here installs configuration or changes live units.
Use the contents of `earlyoom/prefer.regex` with earlyoom `--prefer` and
`earlyoom/avoid.regex` with `--avoid`. These are process-name regexes;
check the installed earlyoom version's comm-name truncation behaviour.
Preference is not immunity and does not override cgroup memory limits.

## Detached daemon option

From the user's login session, not a nested herdr service:

```sh
systemd-run --user --scope --unit=claude-bg \
  -p OOMScoreAdjust=200 claude --bg
```

The user manager creates a sibling scope outside herdr-server's cgroup;
verify `/proc/$PID/cgroup` before relying on isolation. A scope tracks its
processes, not a daemon readiness protocol. For a persistent foreground
daemon use the existing `systemd/claude-daemon.service` template instead:
it has `OOMScoreAdjust=200`, `KillMode=process`, and no embedded secrets.
Do not install or restart either option as part of repository tests.

Pytest `-n auto`/`-n logical` is capped at four below 32 GiB; explicit `-n`
remains an operator choice. On the 15 GiB router host use at most `-n 2`.

## Router-owned live probe script specification

Run only in an approved disposable memory-pressure window, with a second
an authenticated session and an external timeout/cleanup watchdog. Do not run in CI.
1. Record PID, start time, cgroup, oom_score_adj, and health endpoints for
   herdr, keeper, and the daemon; verify the daemon is outside herdr's cgroup.
2. Launch a bounded disposable hog through each writer/seat/review launcher;
   have it fork once and record both scores before allocating. Require >=500
   and unchanged launcher score. An EACCES event is a failed probe, not success.
3. Increase allocation in measured increments under the router's approved
   ceiling until earlyoom kills the hog first. Capture earlyoom journal and
   kernel victim records; abort if service health degrades or the timeout fires.
4. Assert the original herdr/keeper/daemon PID+start identities and health
   survive, and a subsequent writer can start. Kill/reap remaining probe
   descendants by recorded identity and retain timestamped evidence.
Do not claim a global kernel-OOM survival result from a cgroup-only test.
