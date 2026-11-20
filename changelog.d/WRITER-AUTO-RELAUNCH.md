# WRITER-AUTO-RELAUNCH

Native Rust writer supervision now persists a manifest and relaunches failed or
incomplete Codex children at most twice with the same brief after merging fresh
origin/main. Exhaustion and failed relaunch preparation report BLOCKED; review
REQUEST_CHANGES remains terminal. Current-attempt completion events prevent stale
logs from satisfying recovery. Rust fixtures cover SIGKILL, fresh-base visibility,
exit 127, missing result events, the attempt cap and review rejection.

Open follow-up: supervisor/host restart manifest discovery and fenced recovery.
This is the first self-contained child-supervision slice of the larger capability;
restart recovery needs a separate supervisor ownership/admission integration.
