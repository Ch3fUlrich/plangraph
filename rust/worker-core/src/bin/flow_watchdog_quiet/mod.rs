use super::Alert;
use serde::{Deserialize, Deserializer};
use serde_json::Value;
use std::collections::HashMap;

const PREFIX: &str = "busy-quiet ";

#[derive(Default, Deserialize)]
pub struct Reader {
    #[serde(default)]
    session_activity: Block,
    #[serde(default)]
    session_activity_supported: bool,
}

// Only legacy collectors omit this block; capable readers emit null on failure.
#[derive(Default)]
enum Block {
    #[default]
    Absent,
    Present(Value),
}

impl<'de> Deserialize<'de> for Block {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Value::deserialize(deserializer).map(Self::Present)
    }
}

struct Run<'a> {
    run_id: &'a str,
    parent_session: &'a str,
    ended: Option<f64>,
}

struct Runs<'a> {
    records: Vec<Run<'a>>,
    unknown_parents: HashMap<&'a str, usize>,
}

fn timestamp(value: &Value) -> Option<f64> {
    value.as_f64().filter(|v| v.is_finite() && *v >= 0.0)
}

fn read_runs(value: Option<&Value>, now: f64) -> Result<Runs<'_>, Option<usize>> {
    let mut runs = Runs {
        records: Vec::new(),
        unknown_parents: HashMap::new(),
    };
    let values = value.and_then(Value::as_array).ok_or(None)?;
    for (index, value) in values.iter().enumerate() {
        // Only an unreadable parent can invalidate evidence for every session.
        let parent_session = nonempty(value, "parent_session").ok_or(Some(index))?;
        let run = (|| {
            let ended = match value.get("ended")? {
                Value::Null => None,
                value => Some(timestamp(value).filter(|end| *end <= now)?),
            };
            // Live runs only need parent/end evidence; IDs break ended-run ties.
            let run_id = if ended.is_some() {
                nonempty(value, "run_id")?
            } else {
                ""
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
            runs.unknown_parents.entry(parent_session).or_insert(index);
        }
    }
    Ok(runs)
}

fn unknown(context: &str, reason: &str) -> Alert {
    Alert::from(format!("{PREFIX}{context}unknown={reason}"))
}

fn nonempty<'a>(value: &'a Value, field: &str) -> Option<&'a str> {
    value.get(field)?.as_str().filter(|s| !s.is_empty())
}

fn run_diagnostic(alert: &mut Alert, index: Option<usize>) {
    if let Some(index) = index {
        // Diagnostic position is mutable display detail, never an identity.
        alert
            .message
            .push_str(&format!(" run_record_index={index}"));
    }
}

impl Reader {
    pub fn alerts(&self) -> Vec<Alert> {
        let Block::Present(input) = &self.session_activity else {
            return if self.session_activity_supported {
                vec![unknown("", "reader")]
            } else {
                Vec::new()
            };
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
        let mut by_id: HashMap<&str, Vec<&Value>> = HashMap::new();
        let mut distinct = Vec::new();
        for value in sessions {
            if let Some(id) = nonempty(value, "session_id") {
                by_id.entry(id).or_default().push(value);
            } else {
                distinct.push(value);
            }
        }
        for (id, rows) in by_id {
            if rows
                .iter()
                .all(|row| row.get("busy").and_then(Value::as_bool) == Some(false))
            {
                continue;
            }
            if rows.iter().any(|row| {
                ["busy", "role", "transcript_mtime"]
                    .iter()
                    .any(|field| row.get(field) != rows[0].get(field))
            }) {
                let id = serde_json::to_string(id).unwrap();
                alerts.push(unknown(&format!("session={id} "), "sessions"));
                continue;
            }
            distinct.push(rows[0]);
        }
        for value in distinct {
            // Known idle rows need no identity, role or transcript evidence.
            let busy = value.get("busy").and_then(Value::as_bool);
            if busy == Some(false) {
                continue;
            }
            let session_id = nonempty(value, "session_id");
            let context = session_id
                .map(|id| format!("session={} ", serde_json::to_string(id).unwrap()))
                .unwrap_or_default();
            let role = nonempty(value, "role");
            if busy.is_none() || session_id.is_none() || role.is_none() {
                alerts.push(unknown(&context, "sessions"));
                continue;
            }
            let session_id = session_id.unwrap();
            let role = serde_json::to_string(role.unwrap()).unwrap();
            let identity = context;
            let context = format!("{identity}role={role} ");
            let mtime = value
                .get("transcript_mtime")
                .and_then(timestamp)
                .filter(|mtime| *mtime <= now);
            let own_runs: Option<Vec<_>> = runs
                .as_ref()
                .ok()
                .filter(|runs| !runs.unknown_parents.contains_key(session_id))
                .map(|runs| {
                    runs.records
                        .iter()
                        .filter(|run| run.parent_session == session_id)
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
                alert.active = format!("{PREFIX}{identity}unknown={reason}");
                if own_runs.is_none() {
                    let index = match &runs {
                        Ok(runs) => runs.unknown_parents.get(session_id).copied(),
                        Err(index) => *index,
                    };
                    run_diagnostic(&mut alert, index);
                }
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
                    "{PREFIX}{context}quiet_age_seconds={} last_ended_run={last}",
                    (now - mtime).ceil()
                ),
                // Mutable age/role/run details must not refire a standing episode.
                active: format!("{PREFIX}{identity}transcript_mtime={mtime}"),
            });
        }
        // Preserve failed enumeration with no affected busy row, without N+1 noise.
        if !run_unknown_reported
            && runs
                .as_ref()
                .map_or(true, |runs| !runs.unknown_parents.is_empty())
        {
            let index = match &runs {
                Ok(runs) => runs.unknown_parents.values().copied().min(),
                Err(index) => *index,
            };
            let mut alert = unknown("", "runs");
            run_diagnostic(&mut alert, index);
            alerts.push(alert);
        }
        alerts.sort_by(|a, b| a.active.cmp(&b.active));
        alerts.dedup_by(|a, b| a.active == b.active);
        alerts
    }
}

#[cfg(test)]
mod tests;
