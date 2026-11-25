//! Host watchdog for one writer session: round telemetry rows and the 15 minute wait cap.
//!
//! The transcript reader (`round_reader`) names the wait call a writer is blocked in.
//! This module decides, once per tick, whether that wait is over the cap, ends the
//! command of exactly that task through its owner, tells the writer once, and closes the
//! round as BLOCKED the second time. Every step is written to the state file before it
//! acts, so a restart finishes a pending stop, notification or session end instead of
//! repeating it.

pub use super::round_state::{blocked_outcome, closed_rounds, spawn_state_dir, state_file};
use super::round_state::{closed_rounds_of, notice_line, read_state};
pub use super::round_types::{
    Action, Host, Owner, Stop, NOTICE_GRACE_MS, STRIKE_LIMIT, WAIT_LIMIT_MS,
};
use super::{round_reader::Reader, round_time::OpenWait, round_wait::TaskRef};
use pg_worker_core::run::record_io::atomic_write_private;
use serde_json::{json, Map, Value};
use std::{io, path::Path};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Phase {
    /// The stop is written before it is tried; a restart tries it again.
    Stopping,
    /// Termination is confirmed, the writer has not been told.
    Stopped,
    Notified,
}

impl Phase {
    fn name(self) -> &'static str {
        match self {
            Phase::Stopping => "stopping",
            Phase::Stopped => "stopped",
            Phase::Notified => "notified",
        }
    }

    fn parse(text: &str) -> Option<Self> {
        [Phase::Stopping, Phase::Stopped, Phase::Notified]
            .into_iter()
            .find(|phase| phase.name() == text)
    }
}

/// Owners are recorded per task; stops and strikes per wait call.
fn owner_key(round_key: &str, task_id: &str) -> String {
    format!("{round_key}|{task_id}")
}

fn action_key(wait: &OpenWait) -> String {
    format!("{}|{}|{}", wait.round_key, wait.task_id, wait.call_id)
}

/// One session's watchdog: its durable state file and its host.
struct Watch<'a> {
    session: &'a str,
    path: &'a Path,
    host: &'a dyn Host,
    now_ms: i64,
    state: Value,
    done: Vec<Action>,
}

impl Watch<'_> {
    fn save(&self) -> io::Result<()> {
        atomic_write_private(self.path, self.state.to_string().as_bytes())
    }

    fn set_phase(&mut self, key: &str, phase: Phase, wait: &OpenWait) {
        let actions = self.state["actions"]
            .as_object_mut()
            .expect("actions object");
        let entry = actions
            .entry(key.to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        entry["phase"] = json!(phase.name());
        entry["round_key"] = json!(wait.round_key);
        entry["task_id"] = json!(wait.task_id);
        entry["call_id"] = json!(wait.call_id);
        entry["task"] = wait.task.to_json();
        entry["command"] = json!(wait.command);
    }

    fn settle(&mut self, key: &str, phase: &str) -> io::Result<()> {
        self.state["actions"][key]["phase"] = json!(phase);
        self.save()
    }

    /// Waits of this round whose command is confirmed ended: the strikes.
    fn strikes(&self, round_key: &str) -> usize {
        self.state["actions"].as_object().map_or(0, |actions| {
            actions
                .values()
                .filter(|a| a["round_key"] == round_key)
                .filter(|a| matches!(a["phase"].as_str(), Some("stopped" | "notified")))
                .count()
        })
    }

    fn owner_of(&self, key: &str) -> Option<Owner> {
        let record = self.state["owners"][key].as_object()?;
        Some(Owner {
            pid: u32::try_from(record.get("pid")?.as_u64()?).ok()?,
            start_time: record.get("start_time").and_then(Value::as_u64),
        })
    }

    /// Remember who runs each background task the first time it is seen. The host binds a
    /// process to a task by the task's own output file, so the record names that task.
    fn record_owners(&mut self, reader: &Reader) -> io::Result<()> {
        let mut changed = false;
        for task in reader.running() {
            let key = owner_key(&task.round_key, &task.task.id);
            if self.state["owners"][&key].is_object() {
                continue;
            }
            if let Some(owner) = self.host.resolve_owner(self.session, &task.task) {
                self.state["owners"][&key] =
                    json!({"pid": owner.pid, "start_time": owner.start_time});
                changed = true;
            }
        }
        if changed {
            self.save()?;
        }
        Ok(())
    }

    fn finish(&mut self, key: &str, mut phase: Phase, wait: &OpenWait) -> io::Result<()> {
        if phase == Phase::Stopping {
            let Some(owner) = self.owner_of(&owner_key(&wait.round_key, &wait.task_id)) else {
                self.settle(key, "unowned")?;
                self.done.push(Action::OwnerUnknown {
                    task_id: wait.task_id.clone(),
                });
                return Ok(());
            };
            // `signalled` is written only after a stop that left survivors: it records a
            // signal that was really sent. A crash before the signal leaves none, so an
            // owner that then ends by itself is a finished command, not a strike.
            let signalled = self.state["actions"][key]["signalled"] == json!(true);
            // The mark goes into the owner's own output before any signal. A crash after
            // the kill leaves the mark, and mark plus the harness's report of a signal
            // exit is the receipt that settles it; without the mark no signal was sent.
            if self.state["actions"][key]["marked"] != json!(true) {
                if !self.host.mark_stop(&wait.task, key) {
                    self.done.push(Action::StopPending {
                        task_id: wait.task_id.clone(),
                    });
                    return Ok(());
                }
                self.state["actions"][key]["marked"] = json!(true);
                self.state["actions"][key]["marked_ms"] = json!(self.now_ms);
                self.save()?;
            }
            match self.host.stop(&owner, &wait.task, signalled) {
                Stop::Unsignalled => {
                    self.done.push(Action::StopPending {
                        task_id: wait.task_id.clone(),
                    });
                    return Ok(());
                }
                Stop::Survivors => {
                    self.state["actions"][key]["signalled"] = json!(true);
                    self.save()?;
                    self.done.push(Action::StopPending {
                        task_id: wait.task_id.clone(),
                    });
                    return Ok(());
                }
                Stop::Gone => {
                    self.settle(key, "finished")?;
                    self.done.push(Action::Finished {
                        task_id: wait.task_id.clone(),
                    });
                    return Ok(());
                }
                Stop::Ended => {}
            }
            self.set_phase(key, Phase::Stopped, wait);
            self.save()?;
            self.done.push(Action::Stopped {
                task_id: wait.task_id.clone(),
                command: wait.command.clone(),
            });
            phase = Phase::Stopped;
        }
        if phase == Phase::Stopped {
            let count = self.strikes(&wait.round_key);
            let blocked = count >= STRIKE_LIMIT;
            if blocked && self.state["blocked"].is_null() {
                // The round is closed, with its terminal time, before anyone is told:
                // the reader books nothing later and relaunch refuses from here on.
                self.state["blocked"] = json!({
                    "round_key": wait.round_key, "command": super::round_state::redacted(&wait.command),
                    "reason": "second wait over the cap in one round",
                    "closed_ms": self.now_ms, "action": key,
                });
                self.save()?;
                self.done.push(Action::Blocked {
                    round_key: wait.round_key.clone(),
                    command: wait.command.clone(),
                });
            }
            let text = notice_line(wait, count);
            let session = self.session;
            let recipients: &[&str] = if blocked {
                &[session, "routing"]
            } else {
                &[session]
            };
            let sent = recipients.iter().all(|who| {
                self.host
                    .notify(who, &format!("{key}>{who}"), &text)
                    .is_ok()
            });
            if sent {
                if blocked && self.state["blocked"]["action"] == json!(key) {
                    // The BLOCKED notices themselves are what the shutdown waits for.
                    let notices: Vec<Value> = recipients
                        .iter()
                        .map(|who| json!({"recipient": who, "key": format!("{key}>{who}")}))
                        .collect();
                    self.state["blocked"]["notices"] = json!(notices);
                    self.state["blocked"]["notified"] = json!(true);
                    self.state["blocked"]["notified_ms"] = json!(self.now_ms);
                }
                self.set_phase(key, Phase::Notified, wait);
                self.save()?;
                self.done.push(Action::Notified {
                    task_id: wait.task_id.clone(),
                });
            }
        }
        Ok(())
    }

    /// A BLOCKED round ends its live session once its own BLOCKED notices are queued
    /// and delivered: the writer is not stopped before it can read the reason.
    fn end_blocked_round(&mut self) -> io::Result<()> {
        let blocked = &self.state["blocked"];
        if !blocked.is_object() || blocked["ended"] == json!(true) {
            return Ok(());
        }
        let queued = blocked["notified"] == json!(true);
        let undelivered = !queued
            || blocked["notices"]
                .as_array()
                .into_iter()
                .flatten()
                .any(|notice| {
                    self.host.pending(
                        notice["recipient"].as_str().unwrap_or(""),
                        notice["key"].as_str().unwrap_or(""),
                    )
                });
        let round_key = blocked["round_key"].as_str().unwrap_or("").to_owned();
        // The grace runs from the durable BLOCKED transition, whether or not its notices
        // could be queued: a delivery outage must not keep the writer running. Past it the
        // session is ended anyway and the notices stay queued for the bridge to retry.
        let overdue = blocked["closed_ms"]
            .as_i64()
            .is_some_and(|at| self.now_ms - at >= NOTICE_GRACE_MS);
        if undelivered && !overdue {
            return Ok(());
        }
        if self.host.end_session(self.session) {
            self.state["blocked"]["ended"] = json!(true);
            self.save()?;
            if undelivered {
                self.done.push(Action::NoticesOverdue {
                    round_key: round_key.clone(),
                });
            }
            self.done.push(Action::RoundEnded { round_key });
        }
        Ok(())
    }
}

/// One watchdog tick for a session at `now_ms`. `state_path` is the durable action file.
pub fn tick(
    session: &str,
    transcript: &str,
    now_ms: i64,
    state_path: &Path,
    host: &dyn Host,
) -> io::Result<Vec<Action>> {
    let mut watch = Watch {
        session,
        path: state_path,
        host,
        now_ms,
        state: read_state(state_path),
        done: Vec::new(),
    };
    let reader = Reader::read_closed(session, transcript, closed_rounds_of(&watch.state));
    watch.record_owners(&reader)?;
    let open = reader.open_wait(now_ms);
    // A stop or a notification written before a crash is finished first.
    let pending: Vec<(String, Phase, OpenWait)> = watch.state["actions"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(key, a)| {
            let phase = Phase::parse(a["phase"].as_str()?)?;
            let wait = OpenWait {
                round_key: a["round_key"].as_str()?.into(),
                task_id: a["task_id"].as_str()?.into(),
                call_id: a["call_id"].as_str()?.into(),
                task: TaskRef::from_json(&a["task"])?,
                command: a["command"].as_str()?.into(),
                class: super::round_time::Class::Other,
                waited_ms: a["waited_ms"].as_i64().unwrap_or(0),
            };
            (phase != Phase::Notified).then(|| (key.clone(), phase, wait))
        })
        .collect();
    for (key, phase, wait) in pending {
        // A stop is only authorised while the very wait call that earned it is still
        // open: a command that finished, a wait that returned or a new round releases it.
        let same = open.as_ref().is_some_and(|o| {
            o.round_key == wait.round_key && o.task_id == wait.task_id && o.call_id == wait.call_id
        });
        if phase == Phase::Stopping && !same {
            // The wait closed. No new signal is sent. If this stop reached its signal stage
            // (its mark is in the owner's output) the harness's report decides it: a
            // signal exit is the stop, any other exit means the command ended by itself.
            // Until the report arrives the stop stays pending, up to the grace.
            let marked = watch.host.has_mark(&wait.task, &key);
            let since = watch.state["actions"][&key]["marked_ms"]
                .as_i64()
                .unwrap_or(now_ms);
            match (marked, reader.exit_of(&wait.task_id)) {
                (true, Some(137 | 143)) => {
                    watch.set_phase(&key, Phase::Stopped, &wait);
                    watch.save()?;
                    watch.done.push(Action::Stopped {
                        task_id: wait.task_id.clone(),
                        command: wait.command.clone(),
                    });
                    watch.finish(&key, Phase::Stopped, &wait)?;
                }
                (true, None) if now_ms - since < NOTICE_GRACE_MS => {}
                _ => watch.settle(&key, "released")?,
            }
            continue;
        }
        watch.finish(&key, phase, &wait)?;
    }
    if let Some(wait) = open.filter(|w| w.waited_ms > WAIT_LIMIT_MS) {
        let key = action_key(&wait);
        if watch.state["actions"][&key].is_null() {
            watch.set_phase(&key, Phase::Stopping, &wait);
            watch.state["actions"][&key]["waited_ms"] = json!(wait.waited_ms);
            watch.save()?;
            watch.finish(&key, Phase::Stopping, &wait)?;
        }
    }
    watch.end_blocked_round()?;
    Ok(watch.done)
}
