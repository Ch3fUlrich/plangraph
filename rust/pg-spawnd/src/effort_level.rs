use serde_json::{json, Value};
use std::{io::Write, sync::OnceLock};

const LEVELS: [&str; 5] = ["L0", "L1", "L2", "L3", "L4"];
const BUCKETS: [&str; 5] = ["S0", "S1", "S2", "S3", "S4"];

fn profiles() -> &'static Value {
    static PROFILES: OnceLock<Value> = OnceLock::new();
    PROFILES.get_or_init(|| {
        serde_json::from_str(include_str!("../effort_profiles.json"))
            .expect("versioned effort profiles must be valid JSON")
    })
}

fn task_class(bucket: usize, check_kind: &str) -> Result<&'static str, String> {
    match (bucket, check_kind) {
        (0 | 1, "change") => Ok("small_change"),
        (0 | 1, "regression") => Ok("small_regression"),
        (2, "change") => Ok("medium_change"),
        (2, "regression") => Ok("medium_regression"),
        (3 | 4, "change") => Ok("large_change"),
        (3 | 4, "regression") => Ok("large_regression"),
        _ => Err("invalid effort task class inputs".into()),
    }
}

fn rule_level(card: &Value) -> Result<Value, String> {
    let bucket = BUCKETS
        .iter()
        .position(|name| card["bucket"] == *name)
        .ok_or("effort bucket must be S0 through S4")?;
    let risk = card["risk_band"]
        .as_str()
        .ok_or("missing effort risk band")?;
    let risk_floor = profiles()["risk_floors"][risk]
        .as_f64()
        .ok_or("invalid effort risk band")?;
    let risk_level = LEVELS
        .iter()
        .position(|level| {
            profiles()["levels"][*level]["floor"]
                .as_f64()
                .expect("effort profile floor")
                >= risk_floor
        })
        .expect("risk floor must fit an effort profile");
    let tests = card["has_tests"]
        .as_bool()
        .ok_or("missing effort has_tests")?;
    let fanout = card["fanout"].as_u64().ok_or("invalid effort fanout")?;
    let write_count = card["write_count"]
        .as_u64()
        .ok_or("invalid effort write count")?;
    let tokens = card["estimate_tokens"]
        .as_u64()
        .ok_or("invalid effort token estimate")?;
    let token_level = [4_000, 16_000, 32_000, 64_000].partition_point(|limit| tokens > *limit);
    let size_level = |count| [1, 2, 5, 12].partition_point(|limit| count > *limit);
    let features = [
        (bucket, "bucket"),
        (risk_level, "risk_floor"),
        (usize::from(!tests), "missing_tests"),
        (size_level(fanout), "fanout"),
        (size_level(write_count), "write_count"),
        (token_level, "estimate_tokens"),
    ];
    let rule = features.iter().map(|(level, _)| *level).max().unwrap();
    let rule_reason = features
        .iter()
        .filter_map(|(level, reason)| (*level == rule).then_some(*reason))
        .collect::<Vec<_>>()
        .join(", ");
    let assigned = match &card["level"] {
        Value::Null => None,
        value => Some(
            LEVELS
                .iter()
                .position(|name| value == *name)
                .ok_or("assigned effort level must be L0 through L4")?,
        ),
    };
    let chosen = assigned.unwrap_or(rule).max(rule);
    let level = LEVELS[chosen];
    let profile = &profiles()["levels"][level];
    let reason = if chosen > rule {
        "planner_raise"
    } else {
        &rule_reason
    };
    let lowered = assigned
        .filter(|assigned| *assigned < rule)
        .map(|assigned| json!({"assigned":LEVELS[assigned],"reason":rule_reason}));
    Ok(json!({
        "version": profiles()["version"], "level": level, "reason": reason,
        "bucket": BUCKETS[bucket], "risk_band": risk, "risk_floor": risk_floor,
        "task_class": task_class(bucket, card["check_kind"].as_str().unwrap_or("change"))?,
        "floor": profile["floor"], "seats": profile["seats"], "cascade": profile["cascade"],
        "lowered": lowered
    }))
}

pub(crate) fn execute(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    if !args.is_empty() {
        return Err("effort-level takes its card facts as JSON on stdin".into());
    }
    let card = serde_json::from_reader(std::io::stdin().lock()).map_err(|e| e.to_string())?;
    writeln!(out, "{}", rule_level(&card)?).map_err(|e| e.to_string())
}

pub(crate) fn annotate_plan(plan: &mut Value, card: &Value) -> Result<(), String> {
    let Some(features) = plan
        .get("effort_features")
        .filter(|value| value.as_object().is_some_and(|fields| !fields.is_empty()))
    else {
        return Ok(());
    };
    let mut effort = rule_level(features)?;
    if let Some(lowered) = effort["lowered"].as_object_mut() {
        lowered.insert("event".into(), json!("effort_level_lowered"));
        lowered.insert("ts".into(), json!(epoch_secs()));
        lowered.insert(
            "item".into(),
            json!(card["item_key"]
                .as_str()
                .filter(|key| crate::records::valid_item(key))
                .unwrap_or("adhoc")),
        );
    }
    plan["effort"] = effort;
    Ok(())
}

pub(crate) fn epoch_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
