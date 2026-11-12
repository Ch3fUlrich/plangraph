use super::Alert;
use serde::{Deserialize, Deserializer};
use serde_json::Value;
use std::collections::HashMap;

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
    for (index, value) in value
        .and_then(Value::as_array)
        .ok_or(None)?
        .iter()
        .enumerate()
    {
        // Only an unreadable parent can invalidate evidence for every session.
        let parent_session = value
            .get("parent_session")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or(Some(index))?;
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
            runs.unknown_parents.entry(parent_session).or_insert(index);
        }
    }
    Ok(runs)
}

fn unknown(context: &str, reason: &str) -> Alert {
    Alert::from(format!("busy-quiet {context}unknown={reason}"))
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
            if let Some(id) = value
                .get("session_id")
                .and_then(Value::as_str)
                .filter(|id| !id.is_empty())
            {
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
            if rows.iter().any(|row| *row != rows[0]) {
                let id = serde_json::to_string(id).unwrap();
                alerts.push(unknown(&format!("session={id} "), "sessions"));
                continue;
            }
            distinct.push(rows[0]);
        }
        for value in distinct {
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
                .ok()
                .filter(|runs| {
                    !runs
                        .unknown_parents
                        .contains_key(session.session_id.as_str())
                })
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
                if own_runs.is_none() {
                    let index = match &runs {
                        Ok(runs) => runs
                            .unknown_parents
                            .get(session.session_id.as_str())
                            .copied(),
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
mod tests {
    use super::*;
    use serde_json::json;

    fn input() -> Value {
        json!({"now":10000,"sessions":[{"session_id":"session-1","role":"build",
            "busy":true,"transcript_mtime":8740}],
            "runs":[{"run_id":"run-last","parent_session":"session-1","ended":9000}]})
    }

    fn alerts(input: &Value) -> Vec<Alert> {
        Reader {
            session_activity: Block::Present(input.clone()),
            ..Default::default()
        }
        .alerts()
    }

    #[test]
    fn quiet_threshold_has_negative_and_positive_controls() {
        let mut s = input();
        s["runs"] = json!([]); // Empty, readable run evidence still qualifies.
        for seconds in [1140.0, 1200.0, 1201.0, 1200.5, 1200.001] {
            s["sessions"][0]["transcript_mtime"] = (10000.0 - seconds).into();
            let found = alerts(&s);
            if seconds <= 1200.0 {
                assert!(found.is_empty());
            } else {
                assert_eq!(found.len(), 1);
                assert!(found[0].message.contains("quiet_age_seconds=1201 "));
                assert!(found[0].message.ends_with("last_ended_run=none"));
            }
        }
    }

    #[test]
    fn known_idle_rows_skip_missing_empty_or_malformed_session_details() {
        // docs/flow-watchdog.md and the round-1 changelog: skip known idle rows
        // before evidence validation; unreadable transcript is unknown for busy rows.
        for row in [
            json!({"busy":false,"session_id":"session-1","role":"build","transcript_mtime":8740}),
            json!({"busy":false}),
            json!({"busy":false,"session_id":"","role":""}),
            json!({"busy":false,"session_id":null,"role":17,"transcript_mtime":"bad"}),
            json!({"busy":false,"session_id":[],"role":{}}),
        ] {
            let mut s = input();
            s["sessions"] = json!([row]);
            assert!(alerts(&s).is_empty(), "{s}");
        }
    }

    #[test]
    fn indeterminate_idleness_or_invalid_busy_session_details_are_unknown() {
        for row in [
            Value::Null,
            json!({}),
            json!({"busy":"false"}),
            json!({"busy":null}),
            json!({"busy":true}),
            json!({"busy":true,"session_id":"","role":"build"}),
            json!({"busy":true,"session_id":"session-1","role":false}),
        ] {
            let mut s = input();
            s["sessions"] = json!([row]);
            let found = alerts(&s);
            assert_eq!(found.len(), 1);
            assert_eq!(found[0].message, "busy-quiet unknown=sessions");
        }
    }

    #[test]
    fn conflicting_session_rows_report_one_stable_unknown_in_either_order() {
        for (field, conflict) in [
            ("transcript_mtime", json!(8700)),
            ("role", json!("review")),
            ("busy", json!(false)),
            ("transcript_mtime", Value::Null),
        ] {
            let mut s = input();
            let mut other = s["sessions"][0].clone();
            other[field] = conflict;
            s["sessions"].as_array_mut().unwrap().push(other);
            for _ in 0..2 {
                let found = alerts(&s);
                assert_eq!(found.len(), 1);
                assert_eq!(
                    found[0].message,
                    "busy-quiet session=\"session-1\" unknown=sessions"
                );
                assert_eq!(found[0].active, found[0].message);
                s["sessions"].as_array_mut().unwrap().reverse();
            }
        }
    }

    #[test]
    fn condition_local_unknowns_outrank_live_own_run_suppression() {
        for (transcript, runs, reason) in [
            (true, false, "transcript"),
            (false, true, "runs"),
            (true, true, "transcript,runs"),
        ] {
            let mut s = input();
            s["runs"][0]["ended"] = Value::Null;
            if transcript {
                s["sessions"][0]["transcript_mtime"] = Value::Null;
            }
            if runs {
                s["runs"]
                    .as_array_mut()
                    .unwrap()
                    .push(json!({"parent_session":"session-1"}));
            }
            let found = alerts(&s);
            assert_eq!(found.len(), 1);
            assert_eq!(
                found[0].active,
                format!("busy-quiet session=\"session-1\" unknown={reason}")
            );
            s["now"] = json!(10060);
            assert_eq!(alerts(&s)[0].active, found[0].active);
            s["sessions"][0]["transcript_mtime"] = json!(8740);
            if runs {
                s["runs"].as_array_mut().unwrap().pop();
            }
            assert!(alerts(&s).is_empty());
        }
    }

    #[test]
    fn changing_bad_run_record_index_does_not_refire_unknown_evidence() {
        for record in [
            json!({"run_id":"broken"}),
            json!({"parent_session":"session-1"}),
        ] {
            let mut s = input();
            s["runs"].as_array_mut().unwrap().push(record);
            let first = alerts(&s);
            assert_eq!(first.len(), 1);
            assert!(first[0]
                .message
                .ends_with("unknown=runs run_record_index=1"));
            s["runs"].as_array_mut().unwrap().reverse();
            let next = alerts(&s);
            assert!(next[0].message.ends_with("unknown=runs run_record_index=0"));
            assert_eq!(next[0].active, first[0].active);
        }
    }
}
