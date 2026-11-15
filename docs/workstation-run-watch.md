# Workstation run completion

`pg-run-watch` launches a local command with stdout/stderr redirected directly to
an exclusively created log. When the child exits it publishes `LOG.exit` as plain
UTF-8 containing exactly `EXIT <code>` and a newline. The launcher returns the
child's exit code (128 + signal on Unix). It does not decode or search the log:
PowerShell UTF-16 output and arbitrary binary output are safe.

Build on the target host with `cargo build --manifest-path rust/Cargo.toml
-p plangraph-cli --bin pg-run-watch`. Use a fresh log path for every run:

```text
pg-run-watch launch run-unique.log -- codex exec <existing Codex arguments>
pg-run-watch check run-unique.log
```

Pass program arguments separately using the orchestrator's argument-vector API.
Run the launcher on the workstation, without an outer PowerShell log redirection;
keep its process alive until completion. No new SSH/cmd/Select-String chain is
needed. For an existing PowerShell script, launch `powershell.exe -File SCRIPT`
with its existing arguments. The marker belongs next to the log, not in stdout.
Marker and watcher-state writes use persistent per-file lock sidecars.
A failed spawn or marker write reports an error to stderr and returns 2.
A pre-existing log or marker refuses a new launch; never reuse
or delete these files to relaunch a run.

The watcher is a single probe, intended for an orchestrator's existing once-per-
minute scheduler. Capture its stdout **and status** on every probe and relay them
to the operator/result channel. When polling from another host, retrieve the
sidecar and log modification time or execute this binary on the workstation;
never search the UTF-16 log for completion. Interpret statuses as follows:

| Status | Output | Action |
| --- | --- | --- |
| 0 | `EXIT <code>` | Finished, including nonzero child exit; collect result and stop polling |
| 1 | `RUNNING` | No marker; probe again on the next scheduled tick |
| 3 | `NO_CHANGE 60 minutes` | No marker and log unchanged for at least an hour; alert, investigate |
| 2 | stderr diagnostic | Missing log, malformed marker or I/O error; report failure |

A complete marker takes precedence over the quiet warning. The probe saves log
size, modification time and last observed change in `LOG.watch` beside the log
using a per-file lock and atomic replacement. It needs write access there. Size
changes count as activity even when Windows defers timestamp updates. On the first
probe, modification time supplies the baseline; later changes reset the interval. This is an alert,
not a kill or a retry. A launcher killed before publishing completion leaves no
marker; the quiet warning exposes that failure. Old runs launched without this
wrapper need direct investigation; their log contents are not completion proof.

Before trusting a target-host deployment, run:

```text
cargo test --manifest-path rust/Cargo.toml -p plangraph-cli --test run_watch
```

`utf16_finished_and_unfinished_runs_end_to_end` launches a real child, probes it
while unfinished, releases it to exit 7, and probes its plain marker despite
UTF-16 log output. `quiet_run_reports_at_sixty_minutes_and_activity_resets_it`
checks the one-hour warning, resumed activity and finished-marker precedence.
The tests also cover log growth with an unchanged timestamp, malformed markers,
missing logs and stale marker refusal.
Repeat this acceptance on Windows before replacing workstation units. This change
does not install or alter live systemd units or Windows tasks.
