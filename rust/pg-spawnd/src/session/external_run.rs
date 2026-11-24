//! Fleet session registration and roster evidence for lifecycle recovery.

use super::{arguments::Arguments, policy::Admission, process};
use crate::external_run::Registered;
use serde_json::{json, Value};
use std::{io, process::Command, time::Duration};

pub(super) fn register(
    args: &Arguments,
    admission: &Admission,
    name: &str,
    host: &str,
    brief: &super::Admitted,
) -> io::Result<Registered> {
    let state =
        crate::state::resolve_state(None, |key| std::env::var_os(key)).map_err(io::Error::other)?;
    let role = match args.role.rsplit('-').next().unwrap_or_default() {
        "writer" | "implement" => "implement",
        role @ ("fix" | "review" | "judge" | "research") => role,
        _ => "orchestrate",
    };
    Registered::start(
        state,
        json!({
            "run_kind":"fleet-session", "fleet_session_name":name, "host":host, "role":role,
            "harness":"claude", "requested_model":args.model, "effort":args.effort,
            "item_key":admission.fields["card"],
            "brief_items":brief.items, "lane_override":brief.lane_override,
            // `pg-spawnd head-gate --base <admitted_base> --brief <brief_path>` gates the completed head.
            "admitted_base":brief.base, "brief_path":args.prompt,
        }),
    )
}

pub(crate) fn agent_roster() -> io::Result<Vec<Value>> {
    let output = process::capture(
        Command::new("claude").args(["agents", "--json", "--all"]),
        None,
        Duration::from_secs(5),
    )?;
    if !output.status.success() {
        return Err(io::Error::other("fleet agent roster failed"));
    }
    serde_json::from_slice(&output.stdout).map_err(io::Error::other)
}
