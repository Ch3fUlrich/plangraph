use super::{role_of, Caps};
use std::collections::HashMap;

pub(super) const METADATA: [&str; 7] =
    ["owner", "card", "track", "host", "reason", "cap", "handoff"];

#[derive(Debug)]
pub(super) enum ParseError {
    Usage,
    Guard(String),
}

#[derive(Debug)]
pub(super) struct Arguments {
    pub raw: Vec<String>,
    pub name: String,
    pub role: String,
    pub cwd: String,
    pub model: String,
    pub effort: String,
    pub prompt: String,
    pub dry: bool,
    pub respawn: bool,
    pub oom: bool,
    pub guard: Option<Caps>,
    options: HashMap<String, String>,
}

impl Arguments {
    pub fn parse(raw: Vec<String>) -> Result<Self, ParseError> {
        let mut pos = Vec::new();
        let mut options = HashMap::new();
        let (mut dry, mut respawn, mut oom) = (false, false, false);
        let mut iter = raw.iter().cloned().peekable();
        while let Some(arg) = iter.next() {
            match arg.as_str() {
                "--dry-run" => dry = true,
                "--respawn" => respawn = true,
                "--oom-protect" => oom = true,
                _ => {
                    let key = arg.strip_prefix("--").filter(|key| {
                        METADATA.contains(key)
                            || ["mcp", "parent", "ctx-guard", "session"].contains(key)
                    });
                    match (key, iter.peek()) {
                        (Some(key), Some(_)) => {
                            options.insert(key.to_owned(), iter.next().unwrap());
                        }
                        _ => pos.push(arg),
                    }
                }
            }
        }
        let guard = options
            .get("ctx-guard")
            .map(|guard| {
                let parse = || {
                    let (handoff, hard) = guard.split_once(',')?;
                    Caps::new(handoff.trim().parse().ok()?, hard.trim().parse().ok()?)
                };
                parse().ok_or_else(|| ParseError::Guard(guard.clone()))
            })
            .transpose()?;
        let [name, cwd, model, effort, prompt] = pos.try_into().map_err(|_| ParseError::Usage)?;
        Ok(Self {
            raw,
            role: role_of(&name).to_owned(),
            name,
            cwd,
            model,
            effort,
            prompt,
            dry,
            respawn,
            oom,
            guard,
            options,
        })
    }

    pub fn option(&self, key: &str) -> Option<&str> {
        self.options.get(key).map(String::as_str)
    }
}

pub(super) const USAGE: &str = r#"fleet-launch — the standard background Claude launch, plus a launch spec for the recycle watchdog (P1.1).

    fleet-launch <role> <cwd> <model> <effort> <prompt-file> [--mcp <cfg>] [--handoff <path>] [--parent <role>] [--oom-protect] [--ctx-guard N,M] [--respawn] [--dry-run]

Runs `claude --bg` in <cwd> with the fixed fleet flags (strict MCP config, remote-control name, bypassPermissions,
terse style, lean profile ops/fleet-settings.json, unused tools disallowed; AskUserQuestion only for the router) and
CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000 as a backstop behind the context recycle (D-4646). The policy default caps are
700000/800000 since 2026-11-21 (operator: every spawned Claude session up to 700k), above that window, so a default launch
gets its own settings file with the window at 850000 like every other high cap; only a cap at or below 350000 keeps 400000.
The session's context guard caps (AUTOOS_CTX_HANDOFF/HARD) are those of the launch (ops/fleet-launch-policy.json caps, or the
record's --ctx-guard): the lean profile carries the policy default, any other caps go into their own settings file, so the
global default of ~/.claude/settings.json never overrides them (`ctx-cap-check record` flags a live session that differs).
A role whose hard cap (ops/fleet-launch-policy.json caps, or --ctx-guard) leaves less than COMPACT_MARGIN below that
window launches with a settings file <state>/launch-settings/caps-<handoff>-<hard>.json: the lean profile plus env that sets
both compact variables to hard + COMPACT_MARGIN, so guard and compaction agree (planning roles, 700k/800k; the writer
role 900k/950k, operator 2026-11-20: a Claude writer keeps one context up to 1 million tokens instead of restarting at 250k).
Measured 2026-11-12 with the daemon running and user settings env at 400000: a --bg session started with such a
--settings file reads 850000 for both variables (init-0002), and a live session picks the same env up from
<cwd>/.claude/settings.local.json (plan-improve-0421). Precedence for the compact window, highest first: the
--settings file env, the user settings env (~/.claude/settings.json), the daemon's process env; the launch process
env set here reaches a session only when no daemon runs yet.
A dry run writes no settings file and says so. ops/fleet-launch-policy.json refuse_roles (regexes, default none)
names roles that never launch: `(^|-)L2(-|$)`, no L2 sessions (D-5193, D-5196).
Measured: launch env does not reach a bg session when the daemon already runs (daemon env wins),
so the default compact window lives in user settings env; the launch value is only a backstop.
Session names end in a per-host counter (operator 2026-10-31): <role> build-L1 launches as build-L1-0412 and the next
launch or recycle as build-L1-0413 (state file session-seq). The launch record stays keyed by the role: name=<role>
(what the fleet hooks and the inbox use), session_name/seq = the current counter name.
Records ~/.local/state/routing/launch/<name>.json (name, cwd, model, effort, mcp, prompt_file, handoff, parent, children, ts,
session_id) so bin/ctx-watchdog can relaunch the session from its handoff. --respawn (used by the watchdog) refreshes
ts + session_id (and the model/effort of this launch) and keeps prompt_file/handoff/children. --parent <name> names the session's parent (stored as "parent", null if absent; the child's session name is also appended to <state>/launch/children/<parent>.txt; --respawn keeps the old one unless
--parent is given). The claude env carries FLEET_SESSION_NAME=<name>, but `claude --bg` hands off to the daemon and the
session does not inherit it: the fleet hooks resolve the name through session_id, which comes from --bg's output (a full
UUID, or the 8-hex short id resolved via `claude agents --json`). --dry-run prints the command and writes nothing.
The spec may carry oom_protect + ctx_guard (R5, design section 14): --oom-protect and --ctx-guard N,M (positive ints,
hard > handoff) set them, --respawn keeps them; both are applied to the resolved session id after launch (one
oom-protect-sessions line, <ctx-guard-session dir>/<id>.json). The LAST stdout line is `started <session_id>`
(`started-unknown <session_name>` when no id resolved) so the watchdog's recycle confirm never matches a later line.
State dir: $ROUTING_STATE_DIR, else ~/.local/state/routing.
S3 optional metadata flags: --owner, --card (or --track), --host, --reason,
--cap and --handoff. Role/effort use the existing positional arguments. Metadata
is saved in private role and per-session launch specs and inherited on --respawn;
explicit flags override it. For coexisting same-role seats, select the saved counter
name with --session <name> (or the positional name) on --respawn. Legacy respawns
with missing metadata warn rather than stopping the watchdog under enforcement.
ops/fleet-launch-policy.json selects enforcement and the state-relative ledger.
"#;
