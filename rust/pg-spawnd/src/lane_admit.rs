//! `pg-spawnd lane-admit`: the admission of one native writer launch, run by the Python launch
//! boundary (`overflow.native_spawn`) on the host that executes the writer, against that host's
//! state. A request that placement sends to another host is checked there, never here.
use crate::{
    errors::{RefusalKind, ToolError},
    placement::{parameter, Args},
};
use serde_json::Value;
use std::{
    io::{Read, Write},
    path::{Path, PathBuf},
};

const USAGE: &str = "usage: pg-spawnd lane-admit --state PATH (JSON {prompt, card} on stdin)";
/// The refusal reasons of the lane guard, as they open a refusal message.
const REASONS: [&str; 3] = ["lane_unreadable", "lane_collision", "lane_too_many_keys"];

/// The stable reason a refusal message opens with, when the lane guard refused.
pub(crate) fn refusal_reason(message: &str) -> Option<&'static str> {
    REASONS.into_iter().find(|reason| {
        message
            .strip_prefix(reason)
            .is_some_and(|rest| rest.starts_with(':'))
    })
}

/// Admit the launch on stdin against `--state`; print the card to register, which carries the
/// admitted `item_keys` and the mark that they were admitted. Only this verb sets the mark.
pub(crate) fn execute(
    args: &[String],
    mut input: impl Read,
    out: &mut dyn Write,
) -> Result<(), String> {
    let [flag, state] = args else {
        return Err(USAGE.into());
    };
    if flag != "--state" {
        return Err(USAGE.into());
    }
    let mut text = String::new();
    input
        .by_ref()
        .take(4 * 1024 * 1024 + 1)
        .read_to_string(&mut text)
        .map_err(|error| format!("lane-admit input unreadable: {error}"))?;
    if text.len() > 4 * 1024 * 1024 {
        return Err("lane-admit input too large".into());
    }
    let request: Value = serde_json::from_str(&text).map_err(|_| "lane-admit input is not JSON")?;
    let mut args = Args::new();
    for key in ["prompt", "card"] {
        args.insert(key.into(), request[key].clone());
    }
    if !args["prompt"].is_string() || !args["card"].is_object() {
        return Err("lane-admit input needs a string prompt and an object card".into());
    }
    // The options no host state decides are refused before any run record is read.
    crate::native_writer_host::check_launch_card(&args["card"])?;
    let admitted = admit_lane(&PathBuf::from(state), &args)
        .map_err(|error| error.into_error_data().message.into_owned())?;
    writeln!(out, "{}", admitted["card"]).map_err(|error| error.to_string())
}

/// The item keys a launch card names: its singular `item_key` (else `item`, else the key the
/// spawner inherits from `PLANGRAPH_SPAWN_ITEM`, the precedence of the run record) and each
/// member of a batch card, those the record grammar accepts.
pub(crate) fn card_keys(card: &Value, inherited: Option<String>) -> Vec<String> {
    let blank = |value: &Value| value.is_null() || value.as_str() == Some("");
    let singular = [&card["item_key"], &card["item"]];
    let mut keys: Vec<String> = if singular.into_iter().all(blank) {
        inherited.into_iter().collect()
    } else {
        singular
            .into_iter()
            .filter_map(|value| value.as_str().map(str::to_owned))
            .collect()
    };
    keys.extend(
        card["item_keys"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|value| value.as_str().map(str::to_owned)),
    );
    keys.retain(|key| crate::records::valid_item(key));
    keys
}
/// Refuse a native writer for an item key another live writer run holds, naming the run; a run
/// whose liveness cannot be decided refuses with the reason `lane_unreadable`. Check only: the admitted
/// keys replace the card's `item_keys`, with the mark that they were admitted; any `item_keys`
/// or mark the caller sent is dropped.
pub(crate) fn admit_lane(state: &Path, args: &Args) -> Result<Args, ToolError> {
    use pg_spawnd::lane_guard::{admit_launch, LaunchRequest};
    let card = &args["card"];
    let prompt = args["prompt"]
        .as_str()
        .ok_or_else(|| parameter("prompt must be a string"))?;
    let extra = card_keys(card, std::env::var("PLANGRAPH_SPAWN_ITEM").ok());
    let keys = admit_launch(&LaunchRequest {
        prompt,
        extra: &extra,
        state,
    })
    .map_err(|refusal| match refusal.code {
        "batch_input" => parameter(refusal.message),
        code => ToolError::Refused {
            kind: RefusalKind::Lane(code),
            message: refusal.message,
            legs: vec![],
        },
    })?;
    let mut admitted = args.clone();
    // Only this guard may say the keys were admitted; a caller-supplied mark is dropped.
    let card = admitted["card"].as_object_mut().expect("card is an object");
    card.remove("item_keys");
    if !keys.is_empty() {
        card.insert("item_keys".into(), serde_json::json!(keys));
    }
    card.insert("lane_admitted".into(), Value::Bool(true));
    Ok(admitted)
}
