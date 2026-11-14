//! Deterministic research aggregation; no routing or health mutation.
use pg_worker_core::{
    health,
    hosts::Role,
    outcome::{Class, Terminal},
};
use serde_json::{json, Value};
use std::collections::BTreeMap;
#[derive(Debug, PartialEq)]
pub(crate) enum ReportError {
    InvalidWindow,
    InvalidRow,
    Io,
}
impl From<std::io::Error> for ReportError {
    fn from(_: std::io::Error) -> Self {
        Self::Io
    }
}
impl std::fmt::Display for ReportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "health report: {self:?}")
    }
}
fn distribution(mut values: Vec<f64>) -> Value {
    values.sort_by(f64::total_cmp);
    let rank = |p: f64| {
        values
            .get(((values.len() as f64 * p).ceil() as usize).saturating_sub(1))
            .copied()
    };
    json!({"samples":values.len(),"p50":rank(0.5),"p95":rank(0.95)})
}
pub(crate) fn build_report(
    records: &[Value],
    evidence: &[Value],
    since: i64,
    until: i64,
) -> Result<Value, ReportError> {
    if since >= until {
        return Err(ReportError::InvalidWindow);
    }
    let all = crate::health_report_rows::corrected(records, evidence);
    let rows: Vec<_> = all
        .iter()
        .filter(|r| r["ts"].as_i64().is_some_and(|t| t >= since && t < until))
        .collect();
    let (parked, episodes, stale, invalid) =
        crate::health_report_rows::observations(evidence, since, until);
    let (mut hours, mut unusable, mut hang_hours) = (0.0, 0.0, 0.0);
    let (mut max_time, mut missing, mut overrides, mut gated, mut seats) = (0, 0, 0, 0, 0);
    let mut populations = [0usize; 5];
    let mut gate_unknown = 0;
    let mut legs: BTreeMap<String, Vec<health::Row>> = BTreeMap::new();
    for r in &rows {
        let ts = r["ts"].as_i64().ok_or(ReportError::InvalidRow)?;
        let duration = r["duration_s"].as_i64().unwrap_or(0).max(0);
        let h = (ts - ts.saturating_sub(duration).max(since)).max(0) as f64 / 3600.0;
        hours += h;
        let class = r["outcome"].as_str();
        if class != Some("OK") {
            unusable += h;
        }
        if class.is_some_and(|c| c.starts_with("HANG_")) {
            hang_hours += h;
        }
        if class == Some("MAX_TIME") {
            max_time += 1;
        }
        let parsed_class = class.and_then(parse_class);
        if parsed_class.is_none() {
            missing += 1;
            if class.is_none() && r["role"] == "review" {
                seats += 1;
            }
        }
        if r["override"]
            .as_bool()
            .unwrap_or(r["plan"]["p_source"] == "override")
        {
            overrides += 1;
            if r["gate"] == true {
                gated += 1;
            } else if !r["gate"].is_boolean() {
                gate_unknown += 1;
            }
        }
        if class == Some("HANG_SILENT") {
            let bucket = match r["open_tools_at_kill"].as_u64() {
                Some(0) => 1,
                Some(_) if r["s_eff_s"].as_f64().is_some() => 0,
                Some(_) => 3,
                None if r["blind"] == true || r["s_eff_s"] == 600 => 2,
                None => 3,
            };
            populations[bucket] += 1;
        }
        if class == Some("HANG_NO_ACTIVITY") {
            populations[4] += 1;
        }
        let Some(class) = parsed_class else {
            continue;
        };
        let leg = r["leg_key"].as_str().unwrap_or("unknown");
        let role = serde_json::from_value::<Role>(r["role"].clone()).unwrap_or(Role::Research);
        legs.entry(leg.into()).or_default().push(health::Row {
            leg_key: leg.into(),
            route_key: r["route_key"].as_str().unwrap_or("unknown").into(),
            ts,
            role,
            outcome: class,
            run_id: r["run_id"].as_str().unwrap_or("").into(),
            attempt: r["attempt"].as_u64().unwrap_or(0) as u32,
            duration_s: Some(duration),
            reset_at: r["reset_at"].as_i64(),
            reset_assumed: false,
            reset_clamped: false,
            kind: None,
        });
    }
    let mut per_leg = BTreeMap::new();
    for (leg, mut rs) in legs {
        rs.sort_by_key(|r| r.ts);
        let h = health::fold(&leg, &rs[0].route_key, rs[0].role, &rs, until - 1);
        per_leg.insert(leg, json!({"strikes":h.strikes,"state":format!("{:?}",h.state),"until":h.until_s,"raw_reset_at":rs.iter().map(|r|r.reset_at).collect::<Vec<_>>()}));
    }
    let metric = |name: &str| {
        distribution(
            rows.iter()
                .filter_map(|r| r[name].as_f64())
                .filter(|v| v.is_finite() && *v >= 0.0)
                .collect(),
        )
    };
    let known = populations[0] + populations[1];
    let fraction = |n: usize, d: usize| {
        if d == 0 {
            Value::Null
        } else {
            json!(n as f64 / d as f64)
        }
    };
    let kills: Vec<_> = rows
        .iter()
        .filter(|r| {
            r["outcome"]
                .as_str()
                .is_some_and(|c| c.starts_with("HANG_") || c == "NO_PROGRESS")
        })
        .collect();
    let sibling_ok = kills
        .iter()
        .filter(|r| {
            all.iter().any(|s| {
                s["outcome"] == "OK"
                    && s["leg_key"].is_string()
                    && s["leg_key"] == r["leg_key"]
                    && s["run_id"] != r["run_id"]
                    && s["ts"]
                        .as_i64()
                        .zip(r["ts"].as_i64())
                        .is_some_and(|(s, t)| s >= t && s.saturating_sub(t) <= 1800)
            })
        })
        .count();
    let failures: Vec<_> = rows
        .iter()
        .filter(|r| r["outcome"].as_str().is_some_and(|c| c != "OK"))
        .collect();
    let recovered = failures
        .iter()
        .filter(|r| {
            all.iter().any(|s| {
                s["outcome"] == "OK"
                    && s["run_id"].is_string()
                    && s["run_id"] == r["run_id"]
                    && s["attempt"].as_u64() > r["attempt"].as_u64()
                    && s["ts"].as_i64() >= r["ts"].as_i64()
            })
        })
        .count();
    let tool_only = rows
        .iter()
        .filter(|r| r["outcome"] == "TOOL_CALL_ONLY")
        .count();
    Ok(
        json!({"version":1,"witness_alerts":witness_alerts(records, &all, until),"window":{"since":since,"until":until},
        "tool_call_only_floor":{"count":tool_only,"denominator":rows.len(),"fraction":fraction(tool_only,rows.len())},
        "sibling_ok_within30min":{"count":sibling_ok,"denominator":kills.len(),"fraction":fraction(sibling_ok,kills.len())},
        "later_ok_fallthrough_recovery":{"count":recovered,"denominator":failures.len(),"fraction":fraction(recovered,failures.len())},
        "totals":{"launches":rows.len(),"run_hours":hours,"unusable_run_hours":unusable,"hang_hours":hang_hours,"max_time":max_time,"seat_deaths_without_class":seats,
        "overrides":{"total":overrides,"with_gate":gated,"without_gate":overrides-gated-gate_unknown,"unknown_gate":gate_unknown,"gate_denominator":overrides-gate_unknown,"skip_fraction":fraction(overrides-gated-gate_unknown,rows.len())}},
        "per_leg":per_leg,"silent_populations":{"tool_open":populations[0],"tools0":populations[1],"blind":populations[2],"unknown_tools":populations[3],"no_first":populations[4],
        "k_count":populations[0],"k_fraction":fraction(populations[0],known),"unknown_lower_hours":populations[3] as f64*615.01/3600.0,"unknown_upper_hours":populations[3] as f64*1815.01/3600.0,
        "tool_open_savings_hours":populations[0] as f64*(1815.01-615.01)/3600.0,"blind_bound_hours":populations[2] as f64*612.01/3600.0,"no_first_bound_hours":populations[4] as f64*195.01/3600.0},
        "first_activity_s":metric("first_activity_s"),"time_to_first_strong_s":metric("time_to_first_strong_s"),"s_eff_s":metric("s_eff_s"),
        "parked":parked,"no_time_episodes":episodes,"stale_alarms":stale,"baseline":null,"target":null,"missing_fields":{"outcome":missing,"observations":invalid}}),
    )
}
fn witness_alerts(records: &[Value], rows: &[Value], until: i64) -> Vec<String> {
    // One observation per run/leg, even when an attempt was retried or also
    // appears in outcome evidence. A child-writable session store is not a witness.
    let mut runs = BTreeMap::<(String, String), (i64, bool)>::new();
    let mut observe = |row: &Value, parent: &Value| {
        let leg = row["leg_key"]
            .as_str()
            .or(row["leg"]["id"].as_str())
            .or(row["leg"].as_str())
            .or(row["chosen_leg"]["id"].as_str())
            .or(parent["chosen_leg"]["id"].as_str());
        let id = row["run_id"].as_str().or(parent["run_id"].as_str());
        let ts = [
            &row["ts"],
            &row["ended"],
            &parent["ended"],
            &row["started"],
            &parent["started"],
        ]
        .into_iter()
        .find_map(|v| v.as_f64().filter(|t| t.is_finite()).map(|t| t as i64));
        let status = row["status"].as_str().or(parent["status"].as_str());
        if matches!(
            status,
            Some("dry_run" | "skipped" | "refused" | "running" | "queued")
        ) {
            return;
        }
        let (Some(leg), Some(id), Some(ts)) = (leg, id, ts) else {
            return;
        };
        if leg.is_empty() || id.is_empty() {
            return;
        }
        let final_leg = parent["chosen_leg"]["id"].as_str() == Some(leg);
        let model = row["served_model"]
            .as_str()
            .or_else(|| final_leg.then(|| parent["served_model"].as_str()).flatten());
        let kind = row["witness_kind"]
            .as_str()
            .or_else(|| final_leg.then(|| parent["witness_kind"].as_str()).flatten());
        let witnessed = model.is_some_and(|m| !m.is_empty() && m != "unwitnessed")
            && kind != Some("request-side-opencode-store");
        runs.entry((id.into(), leg.into()))
            .and_modify(|r| {
                r.0 = r.0.max(ts);
                r.1 |= witnessed;
            })
            .or_insert((ts, witnessed));
    };
    for record in records {
        if let Some(attempts) = record["attempts"].as_array().filter(|a| !a.is_empty()) {
            for attempt in attempts {
                observe(attempt, record);
            }
        } else {
            observe(record, &Value::Null);
        }
    }
    for row in rows {
        observe(row, &Value::Null);
    }
    let mut per_leg = BTreeMap::<String, [[u64; 2]; 2]>::new();
    for ((_, leg), (ts, witnessed)) in runs {
        if ts < until.saturating_sub(43200) || ts >= until {
            continue;
        }
        let window = usize::from(ts >= until.saturating_sub(21600));
        let counts = &mut per_leg.entry(leg).or_default()[window];
        counts[0] += 1;
        counts[1] += u64::from(witnessed);
    }
    per_leg
        .into_iter()
        .filter_map(|(leg, [before, after])| {
            if before[0] < 5
                || after[0] < 5
                || before[1] * 5 <= before[0] * 4
                || after[1] * 5 >= after[0]
            {
                return None;
            }
            let leg: String = leg
                .chars()
                .map(|c| if c.is_control() { '_' } else { c })
                .collect();
            Some(format!(
                "ALERT witnessed share collapsed: leg={leg} previous={}/{} recent={}/{}",
                before[1], before[0], after[1], after[0]
            ))
        })
        .collect()
}

fn parse_class(text: &str) -> Option<Class> {
    Some(match text {
        "OK" => Class::Ok,
        "MAX_TIME" => Class::MaxTime,
        "HANG_SILENT" => Class::HangSilent,
        "HANG_NO_ACTIVITY" => Class::HangNoActivity,
        "CRASH" => Class::Crash,
        "EMPTY" => Class::Empty,
        "NO_PROGRESS" => Class::NoProgress,
        "KILLED_EXTERNAL" => Class::KilledExternal,
        "TOOL_CALL_ONLY" => Class::ToolCallOnly,
        "NO_VERDICT" => Class::NoVerdict,
        "QUOTA" => Class::Quota,
        "RATE_LIMIT" => Class::RateLimit,
        "AUTH" => Class::Auth,
        "MODEL_UNAVAILABLE" => Class::ModelUnavailable,
        "PROVIDER_UNAVAILABLE" => Class::ProviderUnavailable,
        "caller_fault" => Class::Neutral(Terminal::CallerFault),
        "cancelled" => Class::Neutral(Terminal::Cancelled),
        "orphaned" => Class::Neutral(Terminal::Orphaned),
        "git_config_mutated" => Class::Neutral(Terminal::GitConfigMutated),
        "writer_no_changes" => Class::Neutral(Terminal::WriterNoChanges),
        "kill_incomplete" => Class::Neutral(Terminal::KillIncomplete),
        "proof_failure" => Class::Neutral(Terminal::ProofFailure),
        _ => return None,
    })
}
#[cfg(test)]
#[path = "health_report_tests.rs"]
mod tests;
