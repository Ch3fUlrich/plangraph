Detached orchestrator watchers now use `plangraph watcher-launch JOB NAME -- COMMAND
[ARG...]` to submit a transient systemd user service. Commands and working directories
are saved before submission; services retain output logs and atomically publish exit
codes or execution errors. Duplicate submission and completed-command replay are refused.
`plangraph resume-digest JOB` lists pending units and completed results without consuming
them. Spawner and orchestration skills require startup recovery and place authorized
wait continuations inside the service so a session kill cannot discard a pending action.
Incomplete watcher records are reported individually so interrupted admission cannot
hide another watcher's completed outcome.
Requires Linux user systemd; no session-child fallback is provided. Host reboot and
explicit service termination recovery remain manual (pending records retain unit IDs).
Host regression: `cargo test -p plangraph-cli --features systemd-tests --test watchers`
from `rust/` kills the submitting process group while a real service waits, then
checks that its continuation and result survive. Ordinary tests need no user manager.
