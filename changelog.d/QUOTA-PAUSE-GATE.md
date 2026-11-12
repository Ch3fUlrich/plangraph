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
