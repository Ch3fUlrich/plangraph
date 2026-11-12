use super::Alert;
use serde::{Deserialize, Deserializer};
use serde_json::Value;
use std::collections::HashSet;

// Only legacy collectors omit this block; implemented readers emit null on failure.
#[derive(Default)]
pub enum Reader {
    #[default]
    Absent,
    Present(Value),
}

impl<'de> Deserialize<'de> for Reader {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Value::deserialize(deserializer).map(Self::Present)
    }
}

#[derive(Deserialize)]
struct Session {
    session_id: String,
    role: String,
    #[serde(default)]
    transcript_mtime: Value,
}

struct Run<'a> {
    run_id: &'a str,
    parent_session: &'a str,
    ended: Option<f64>,
}

struct Runs<'a> {
    records: Vec<Run<'a>>,
    unknown_parents: HashSet<&'a str>,
}

fn timestamp(value: &Value) -> Option<f64> {
    value.as_f64().filter(|v| v.is_finite() && *v >= 0.0)
}

fn read_runs(value: Option<&Value>, now: f64) -> Option<Runs<'_>> {
    let mut runs = Runs {
        records: Vec::new(),
        unknown_parents: HashSet::new(),
    };
    for value in value?.as_array()? {
        // Only an unreadable parent can invalidate evidence for every session.
        let parent_session = value
            .get("parent_session")?
            .as_str()
            .filter(|s| !s.is_empty())?;
        let run = (|| {
            let run_id = value.get("run_id")?.as_str().filter(|s| !s.is_empty())?;
            let ended = match value.get("ended")? {
                Value::Null => None,
                value => Some(timestamp(value).filter(|end| *end <= now)?),
            };
            Some(Run {
                run_id,
                parent_session,
                ended,
            })
        })();
        if let Some(run) = run {
            runs.records.push(run);
        } else {
            runs.unknown_parents.insert(parent_session);
        }
    }
    Some(runs)
}

fn unknown(context: &str, reason: &str) -> Alert {
    Alert::from(format!("busy-quiet {context}unknown={reason}"))
}

impl Reader {
    pub fn alerts(&self) -> Vec<Alert> {
        let Self::Present(input) = self else {
            return Vec::new();
        };
        if !input.is_object() {
            return vec![unknown("", "reader")];
        }
        let Some(now) = input.get("now").and_then(timestamp) else {
            return vec![unknown("", "clock")];
        };
        let Some(sessions) = input.get("sessions").and_then(Value::as_array) else {
            return vec![unknown("", "sessions")];
        };
        let runs = read_runs(input.get("runs"), now);
        let mut alerts = Vec::new();
        let mut run_unknown_reported = false;
        for value in sessions {
            // Known idle rows need no identity, role or transcript evidence.
            match value.get("busy").and_then(Value::as_bool) {
                Some(false) => continue,
                Some(true) => (),
                None => {
                    alerts.push(unknown("", "sessions"));
                    continue;
                }
            }
            let Ok(session) = serde_json::from_value::<Session>(value.clone()) else {
                alerts.push(unknown("", "sessions"));
                continue;
            };
            if session.session_id.is_empty() || session.role.is_empty() {
                alerts.push(unknown("", "sessions"));
                continue;
            }
            let id = serde_json::to_string(&session.session_id).unwrap();
            let role = serde_json::to_string(&session.role).unwrap();
            let context = format!("session={id} role={role} ");
            let mtime = timestamp(&session.transcript_mtime).filter(|mtime| *mtime <= now);
            let own_runs: Option<Vec<_>> = runs
                .as_ref()
                .filter(|runs| !runs.unknown_parents.contains(session.session_id.as_str()))
                .map(|runs| {
                    runs.records
                        .iter()
                        .filter(|run| run.parent_session == session.session_id)
                        .collect()
                });
            let reason = match (mtime, &own_runs) {
                (None, None) => Some("transcript,runs"),
                (None, _) => Some("transcript"),
                (_, None) => Some("runs"),
                _ => None,
            };
            if let Some(reason) = reason {
                run_unknown_reported |= own_runs.is_none();
                let mut alert = unknown(&context, reason);
                alert.active = format!("busy-quiet session={id} unknown={reason}");
                alerts.push(alert);
                continue;
            }
            let mtime = mtime.unwrap();
            let own_runs = own_runs.unwrap();
            if now - mtime <= 20.0 * 60.0 || own_runs.iter().any(|run| run.ended.is_none()) {
                continue;
            }
            let last = own_runs
                .iter()
                .filter_map(|run| run.ended.map(|end| (end, run.run_id)))
                .max_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.1.cmp(b.1)))
                .map(|(_, id)| serde_json::to_string(id).unwrap())
                .unwrap_or_else(|| "none".into());
            alerts.push(Alert {
                message: format!(
                    "busy-quiet {context}quiet_age_seconds={} last_ended_run={last}",
                    (now - mtime).ceil()
                ),
                // Mutable age/role/run details must not refire a standing episode.
                active: format!("busy-quiet session={id} transcript_mtime={mtime}"),
            });
        }
        // Preserve failed enumeration with no affected busy row, without N+1 noise.
        if !run_unknown_reported
            && runs
                .as_ref()
                .is_none_or(|runs| !runs.unknown_parents.is_empty())
        {
            alerts.push(unknown("", "runs"));
        }
        alerts.sort_by(|a, b| a.active.cmp(&b.active));
        alerts.dedup_by(|a, b| a.active == b.active);
        alerts
    }
}
