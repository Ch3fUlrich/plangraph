//! Host watchdog for one writer session: round telemetry rows and the 15 minute wait cap.
//!
//! The transcript reader (`round_time`) names the wait a writer is blocked in. This
//! module decides, once per tick, whether that wait is over the cap, ends the command
//! through its owner record, tells the writer once, and ends the round as BLOCKED the
//! second time. Every step is written to the state file before it acts, so a restart
//! finishes a pending stop or notification instead of repeating it.

use super::{round_reader::Reader, round_time::OpenWait};
use pg_worker_core::run::record_io::atomic_write_private;
use serde_json::{json, Map, Value};
use std::{fs, io, path::Path};

/// Longest wait on one background command before the watchdog ends it.
pub const WAIT_LIMIT_MS: i64 = 15 * 60_000;
/// Waits ended in one round before the round is BLOCKED.
pub const STRIKE_LIMIT: usize = 2;

/// The process that runs a background command, found on the executing host.
#[derive(Clone, Debug, PartialEq)]
pub struct Owner {
    pub pid: u32,
    pub start_time: Option<u64>,
}

/// What a stop found.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Stop {
    /// The recorded process is gone or is no longer that command: nothing was signalled.
    Gone,
    /// The watchdog signalled it and nothing of it is left.
    Ended,
    Survivors,
}

/// Effects of the watchdog. The real host reads /proc and signals; tests fake it.
pub trait Host {
    /// The one live process of `session` that runs `command`; none when it is gone or ambiguous.
    fn resolve_owner(&self, session: &str, command: &str) -> Option<Owner>;
    /// End the recorded owner and its process group, only while that very process
    /// (same start time) still runs `command`.
    fn stop(&self, owner: &Owner, command: &str) -> Stop;
    /// Queue one line for `recipient`; `key` makes a second queue of it a no-op.
    fn notify(&self, recipient: &str, key: &str, line: &str) -> io::Result<()>;
}

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

/// What one tick did, for the log.
#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    Stopped {
        task_id: String,
        command: String,
    },
    StopPending {
        task_id: String,
    },
    /// The command finished by itself before the stop: no strike, no notice.
    Finished {
        task_id: String,
    },
    /// No recorded owner: nothing is signalled on a guess.
    OwnerUnknown {
        task_id: String,
    },
    Notified {
        task_id: String,
    },
    Blocked {
        round_key: String,
        command: String,
    },
}

fn action_key(round_key: &str, task_id: &str) -> String {
    format!("{round_key}|{task_id}")
}

fn read_state(path: &Path) -> Value {
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({"actions": {}}))
}

fn write_state(path: &Path, state: &Value) -> io::Result<()> {
    atomic_write_private(path, state.to_string().as_bytes())
}

fn phase_of(state: &Value, key: &str) -> Option<Phase> {
    Phase::parse(state["actions"][key]["phase"].as_str()?)
}

fn set_phase(state: &mut Value, key: &str, phase: Phase, wait: &OpenWait) {
    let actions = state["actions"].as_object_mut().expect("actions object");
    let entry = actions
        .entry(key.to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    entry["phase"] = json!(phase.name());
    entry["round_key"] = json!(wait.round_key);
    entry["task_id"] = json!(wait.task_id);
    entry["command"] = json!(wait.command);
}

/// Waits of this round whose command is confirmed ended: the strikes.
fn strikes(state: &Value, round_key: &str) -> usize {
    state["actions"].as_object().map_or(0, |actions| {
        actions
            .values()
            .filter(|a| a["round_key"] == round_key)
            .filter(|a| matches!(a["phase"].as_str(), Some("stopped" | "notified")))
            .count()
    })
}

fn line(wait: &OpenWait, strikes: usize) -> String {
    let minutes = wait.waited_ms / 60_000;
    let first = format!(
        "Watchdog ended your background command `{}` (task {}) after {minutes} min of waiting; \
         the cap is {} min. Do not wait on one command that long: split it or run it in the \
         foreground with a timeout.",
        wait.command,
        wait.task_id,
        WAIT_LIMIT_MS / 60_000
    );
    if strikes >= STRIKE_LIMIT {
        format!(
            "BLOCKED: second wait over the cap in this round, last command `{}`. {first}",
            wait.command
        )
    } else {
        first
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
    let reader = Reader::read(session, transcript);
    let mut state = read_state(state_path);
    let mut done = Vec::new();
    record_owners(session, &reader, &mut state, host, state_path)?;
    let open = reader.open_wait(now_ms);
    // A stop or a notification written before a crash is finished first.
    let pending: Vec<(String, Phase, OpenWait)> = state["actions"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(key, a)| {
            let phase = Phase::parse(a["phase"].as_str()?)?;
            let wait = OpenWait {
                round_key: a["round_key"].as_str()?.into(),
                task_id: a["task_id"].as_str()?.into(),
                command: a["command"].as_str()?.into(),
                class: super::round_time::Class::Other,
                waited_ms: a["waited_ms"].as_i64().unwrap_or(0),
            };
            (phase != Phase::Notified).then(|| (key.clone(), phase, wait))
        })
        .collect();
    for (key, phase, wait) in pending {
        // A stop is only authorised while the same wait is still open: a command that
        // finished, a wait that returned or a new round releases it without a strike.
        let same = open
            .as_ref()
            .is_some_and(|o| o.round_key == wait.round_key && o.task_id == wait.task_id);
        if phase == Phase::Stopping && !same {
            state["actions"][&key]["phase"] = json!("released");
            write_state(state_path, &state)?;
            continue;
        }
        finish(
            session, &key, phase, &wait, &mut state, host, state_path, &mut done,
        )?;
    }
    if let Some(wait) = open.filter(|w| w.waited_ms > WAIT_LIMIT_MS) {
        let key = action_key(&wait.round_key, &wait.task_id);
        if phase_of(&state, &key).is_none() && state["actions"][&key].is_null() {
            set_phase(&mut state, &key, Phase::Stopping, &wait);
            state["actions"][&key]["waited_ms"] = json!(wait.waited_ms);
            write_state(state_path, &state)?;
            finish(
                session,
                &key,
                Phase::Stopping,
                &wait,
                &mut state,
                host,
                state_path,
                &mut done,
            )?;
        }
    }
    Ok(done)
}

/// Remember who runs each background command the first time it is seen, so a stop
/// later goes to that recorded process and never to whatever matches then.
fn record_owners(
    session: &str,
    reader: &Reader,
    state: &mut Value,
    host: &dyn Host,
    state_path: &Path,
) -> io::Result<()> {
    let mut changed = false;
    for task in reader.running() {
        let key = action_key(&task.round_key, &task.task_id);
        if state["owners"][&key].is_object() {
            continue;
        }
        if let Some(owner) = host.resolve_owner(session, &task.command) {
            state["owners"][&key] = json!({"pid": owner.pid, "start_time": owner.start_time});
            changed = true;
        }
    }
    if changed {
        write_state(state_path, state)?;
    }
    Ok(())
}

fn owner_of(state: &Value, key: &str) -> Option<Owner> {
    let record = state["owners"][key].as_object()?;
    Some(Owner {
        pid: u32::try_from(record.get("pid")?.as_u64()?).ok()?,
        start_time: record.get("start_time").and_then(Value::as_u64),
    })
}

fn settle(state: &mut Value, key: &str, phase: &str, state_path: &Path) -> io::Result<()> {
    state["actions"][key]["phase"] = json!(phase);
    write_state(state_path, state)
}

#[allow(clippy::too_many_arguments)]
fn finish(
    session: &str,
    key: &str,
    mut phase: Phase,
    wait: &OpenWait,
    state: &mut Value,
    host: &dyn Host,
    state_path: &Path,
    done: &mut Vec<Action>,
) -> io::Result<()> {
    if phase == Phase::Stopping {
        let Some(owner) = owner_of(state, key) else {
            settle(state, key, "unowned", state_path)?;
            done.push(Action::OwnerUnknown {
                task_id: wait.task_id.clone(),
            });
            return Ok(());
        };
        // The signal is written down first: a crash after it leaves a record that
        // lets the next tick tell "ended by us" from "ended by itself".
        let signalled = state["actions"][key]["signalled"] == json!(true);
        state["actions"][key]["signalled"] = json!(true);
        write_state(state_path, state)?;
        let result = match host.stop(&owner, &wait.command) {
            Stop::Gone if signalled => Stop::Ended,
            other => other,
        };
        match result {
            Stop::Survivors => {
                done.push(Action::StopPending {
                    task_id: wait.task_id.clone(),
                });
                return Ok(());
            }
            Stop::Gone => {
                settle(state, key, "finished", state_path)?;
                done.push(Action::Finished {
                    task_id: wait.task_id.clone(),
                });
                return Ok(());
            }
            Stop::Ended => {}
        }
        set_phase(state, key, Phase::Stopped, wait);
        write_state(state_path, state)?;
        done.push(Action::Stopped {
            task_id: wait.task_id.clone(),
            command: wait.command.clone(),
        });
        phase = Phase::Stopped;
    }
    if phase == Phase::Stopped {
        let count = strikes(state, &wait.round_key);
        let blocked = count >= STRIKE_LIMIT;
        if blocked && state["blocked"].is_null() {
            // The round is closed before anyone is told: relaunch refuses from here on.
            state["blocked"] = json!({
                "round_key": wait.round_key, "command": wait.command,
                "reason": "second wait over the cap in one round",
            });
            write_state(state_path, state)?;
            done.push(Action::Blocked {
                round_key: wait.round_key.clone(),
                command: wait.command.clone(),
            });
        }
        let text = line(wait, count);
        let recipients: &[&str] = if blocked {
            &[session, "routing"]
        } else {
            &[session]
        };
        let sent = recipients
            .iter()
            .all(|who| host.notify(who, &format!("{key}>{who}"), &text).is_ok());
        if sent {
            set_phase(state, key, Phase::Notified, wait);
            write_state(state_path, state)?;
            done.push(Action::Notified {
                task_id: wait.task_id.clone(),
            });
        }
    }
    Ok(())
}

/// The durable BLOCKED outcome of a session's round, if the watchdog wrote one.
pub fn blocked_outcome(state_path: &Path) -> Option<Value> {
    let state = read_state(state_path);
    state["blocked"]
        .is_object()
        .then(|| state["blocked"].clone())
}

/// The action file of one session under a spawn state directory.
pub fn state_file(spawn_state: &Path, session: &str) -> std::path::PathBuf {
    spawn_state
        .join("round-watch")
        .join(format!("{session}.json"))
}

/// Spawn state directory, resolved as the watchdog and the session store resolve it.
pub fn spawn_state_dir() -> std::path::PathBuf {
    if let Some(dir) = std::env::var_os("PLANGRAPH_SPAWN_STATE").filter(|v| !v.is_empty()) {
        return dir.into();
    }
    let base = std::env::var_os("XDG_STATE_HOME")
        .filter(|v| !v.is_empty())
        .map(std::path::PathBuf::from)
        .or_else(|| std::env::home_dir().map(|home| home.join(".local/state")))
        .unwrap_or_default();
    base.join("plangraph")
}
