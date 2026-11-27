# LE-WS-BUILD-SSH-KEX-RESET-BLOCKS-ORCHESTRATION

Outcome. The coordinator's per-minute SSH link check tells a pre-banner kex reset from an outage, retries it once, and raises one routing alert per episode naming the server-side cause when resets repeat.

- plan_source. Keeper item plangraph:LE-WS-BUILD-SSH-KEX-RESET-BLOCKS-ORCHESTRATION, 2026-11-27 04:52Z, priority 10, D-7060; incidents 05:17Z, 06:16Z, 14:06Z, 14:20Z (cause found).
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 9cc5f5b2310562ef5fed144afed58cfffcff323626fcebe74d2c51e69b0b278b
- tree SHA-256. c3a679cc5387d4687e6b440d5267ca1cc418ec338fe2f3cdf47fac0c9a179299
- evidence_after_deploy. Host steps, not code: (1) the workstation host session, with admin rights, stops the stuck pre-auth `sshd.exe` children or restarts the Windows OpenSSH service, and records the sshd log cause (Event Viewer OpenSSH/Operational) in the item evidence; (2) 20 consecutive `ssh ws-build true` calls spaced 30 s from coding.vm succeed under normal fleet load. After the new `pg-host-link` reaches coding.vm (`pg-host-ssh-link@ws-build.timer`): record one `host-ssh-kex-reset` routing message from a staged episode, or the unit's journal lines `kex_reset` with no alert below the threshold.

## Checked against base

- Cause (incident 14:20Z): Windows OpenSSH 9.5 on the workstation; 18 `sshd.exe` children stuck before authentication; 28 of 60 local no-auth probes dropped before the banner; default `MaxStartups 10:30:100`. Clearing the children or restarting the service needs Windows admin rights, so it is a host step.
- `pg-host-link ssh-tick` runs once a minute on coding.vm per worker (`pg-host-ssh-link@.timer`, `OnCalendar=minutely`) and calls `probe_ssh(destination)`, which reads only the exit code: 255 is unreachable, the stderr text is dropped. [deploy/systemd/pg-host-ssh-link@.service; deploy/systemd/pg-host-ssh-link@.timer; rust/worker-core/src/host_link.rs:43-54; rust/worker-core/src/bin/pg-host-link.rs:56-83]
- Two failed probes in a row raise `host-ssh-down`, so an intermittent kex reset reads as an outage or not at all; no state counts resets. [rust/worker-core/src/host_link.rs:236-333]
- `capture` already returns the bounded stderr of the probe. [rust/worker-core/src/host_command.rs:92-104]
- The bridge posts host-link alerts to routing as keeper messages and accepts a fixed rule list. [rust/plangraph-cli/src/host_link_bridge.rs:76-148]

## Decisions

- D1. Classification. `probe_ssh_detail(destination) -> io::Result<Probe { reachable: bool, kex_reset: bool }>` runs the same command as `probe_ssh`. `kex_reset` is true when the exit code is 255 and stderr contains `kex_exchange_identification`, or has a line matching `^Connection (reset|closed) by \S+ port [0-9]+` and no `Permission denied` / `Host key verification failed` line. `probe_ssh` stays as a wrapper returning `reachable` for its other callers.
- D2. Retry. In `ssh_tick`, a probe with `kex_reset` is repeated once after 2 s (the incidents: a second try a few seconds later succeeded every time); 7 s + 2 s + 7 s stays inside the unit's `TimeoutStartSec=20s`. The tick's `reachable` is the result of the last probe; each `kex_reset` probe is counted.
- D3. Reset counting. `host_link::observe_kex(root, destination, now, resets: u32, evidence) -> io::Result<Option<u64>>` keeps `watchdog/host-link/kex-<sha256 of {"kex": destination}>.json` with `resets: Vec<u64>` (timestamps inside the window) and `episode_since: Option<u64>`, `alerted: bool`. Window 900 s; alert when the window holds at least 3 resets and the episode is not yet alerted; the episode ends after 1800 s without a reset, which clears `alerted`. Writes are atomic and private like the other link states; a clock moving backwards is an error, as in `Link::observe`.
- D4. EXPERIMENT (rule 3.76) for 3 resets in 900 s: incident rates were 7 of about 20 calls in 35 min, 4 of 6 in 10 min and 7 of 12 in 39 min, against zero resets in normal operation; at one probe a minute (plus one retry) such an episode crosses the threshold within about 5-9 minutes. Measure alerts per week against the host session's sshd findings; tune the two constants in `host_link.rs`.
- D5. Alert. Rule `host-ssh-kex-reset`, enqueued through the existing `Link::enqueue`-style writer with `kind: "alert"`, `recipient: "routing"`, `item_key: "plangraph:LE-WS-BUILD-SSH-KEX-RESET-BLOCKS-ORCHESTRATION"`, `host`, `observer`, `destination`, `resets_in_window`, `window_s: 900`, `down_since: episode_since`, `event_id: sha256("kex:<state key>:<episode_since>")`. The bridge accepts the rule and posts: `<host>: <n> SSH connections reset before the server banner within 900 s (episode since <since>); the cause is on the server: sshd connection limit (MaxStartups) or stuck pre-auth sshd children; read the sshd log and restart sshd on <host>`. The text carries the count of the alerting tick only once per episode, so re-delivery of the same file keeps one keeper message.
- D6. Output. `ssh_tick` prints `kex_reset <destination> count <n> retry <ok|failed>` for a tick with resets and `<host> kex resets <n> in 900 s since <since>` when it queues the alert, beside today's lines.
- D7. The Windows-side recurrence guard (counting and clearing stuck pre-auth `sshd.exe` children) runs with admin rights on the workstation; it is a host task for the workstation host session and not part of this code item. This item gives the coordinator's detection and the routed cause.

## Entry points

- E1. `pg-host-link ssh-tick STATE OBSERVER HOST DESTINATION PEER` (the `pg-host-ssh-link@.service` unit).
- E2. `host-link-deliver` (the watchdog bridge) for rule `host-ssh-kex-reset`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A fake `ssh` that exits 255 with `kex_exchange_identification: read: Connection reset by peer` on the first call and 0 on the second: the tick is reachable, prints `kex_reset ... retry ok`, queues no `host-ssh-down`. | X1 |
| B2 | E1 | A fake `ssh` that exits 255 with `Permission denied (publickey)`: not a kex reset (`kex_reset` false), existing unreachable handling unchanged. | X1 |
| B3 | E1 | Three ticks with one reset each inside 900 s queue exactly one `host-ssh-kex-reset` event; further resets in the same episode queue none; after 1800 s without a reset, a new episode can alert again. | X2 |
| B4 | E2 | The bridge posts one keeper message to routing whose text names the host, the count and the server-side cause, `msg_id` = `event_id`. | X3 |

## Verification

Write X1-X3 first; on the base X1 sees `host-ssh-down` paths and no `kex_reset` line, X2/X3 do not compile (`observe_kex`, rule unknown).

- X1. `rust/worker-core/tests/host_link.rs::ssh_tick_retries_a_kex_reset_and_keeps_auth_failures_apart` (fake `ssh` on `PATH` via `executable_test_support::write_private_executable`, as in the existing ssh-tick test, with a counter file to switch answers).
- X2. `rust/worker-core/tests/host_link.rs::repeated_kex_resets_alert_once_per_episode` (calls `observe_kex` with injected times).
- X3. `rust/plangraph-cli/tests/host_link_bridge.rs::kex_reset_alert_reaches_keeper_with_its_cause` (pattern of `disk_hygiene_zero_progress_reaches_keeper`).
- X4. Held out: `cargo test -p pg-worker-core --test host_launch` (it reads the SSH link state through `observe_ssh` / `ssh_down_since`) stays green.

## Scope and reuse

- R1. Reuse `capture`, the link state layout, atomic private writes, the outbox writer, the bridge delivery and the keeper's `msg_id` dedupe.
- N1. No sshd configuration, firewall, Tailscale or Windows change; no change to `ssh-wait`, to `host-ssh-down` timing or to the unit files. The Windows guard (D7) and the sshd restart are host steps.

### File budgets

Per-file caps are in the card `line_caps` and the TSV. plangraph:LE-NO-PROGRESS-ALARM-FOR-AN-IDLE-FLEET writes `host_link.rs`, `host_link_bridge.rs`, its test and `docs/host-link.md` too; the keeper's `write_overlap` gate runs the two lanes one after the other, and the caps here hold on top of either order.

### Implementation

- host_link.rs. `Probe`, `probe_ssh_detail`, the D1 classifier as a pure `fn kex_reset(code: Option<i32>, stderr: &[u8]) -> bool`, `observe_kex` and its state per D3/D5, `pub const HOST_SSH_KEX_RESET: &str = "host-ssh-kex-reset"`.
- bin/pg-host-link.rs. `ssh_tick` per D2/D6, calling `observe_kex` after `observe_ssh`.
- host_link_bridge.rs. Accept `HOST_SSH_KEX_RESET` in `run`; D5 text in `deliver`.
- docs/host-link.md. The kex-reset classification, retry, threshold, episode and the routed cause.
- changelog one line.

## Open points for design approval

None.
