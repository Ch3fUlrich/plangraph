## D-5149: quota pause admission

The usage owner/router creates `<spawner-state>/quota-pause` at authoritative
USAGE >=95% and removes it at reset. Contents are ignored; the gate never clears
it based on estimated transcript usage. State is `PLANGRAPH_SPAWN_STATE`, falling
back to `${XDG_STATE_HOME:-$HOME/.local/state}/plangraph`.

Session launch and relaunch refuse with `quota pause` before invoking the action
or stopping an existing session. Claude-prefixed seat/judge legs also refuse;
`basic-analysis` is the explicit exempt role (including numbered session names).
Other providers remain available. The deployed ctx-watchdog skips its external
router invocation with one alert line while paused. Redeploy the service template
to apply that guard; the external usage owner remains responsible for the flag.

Quota flag reads now fail closed on filesystem errors, and empty state/XDG values
use environment/home defaults. Mapping legs and unknown-provider metadata cannot
bypass a pause; exempt session names accept any numeric suffix. Seat mixins use
`state` or `_state`, and probe admission uses the configured runner/store root.
Launch-spec reports `quota_pause` (its `state` and `role` fields remain required).
The watchdog also skips dangling symlinks and uses systemd `%h` when XDG is unset.
Hermetic regressions cover dry-run refusals, pre-stop relaunch refusal, launch-spec
build/reset, and real admission/probe resumption after reset.
