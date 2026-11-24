//! The scheduled `pg-merge-close` units: a pass whose keeper index refresh is still running ends
//! as its own state, counted in a streak the next run continues; the third in a row fails.
//!
//! The streak lives in one private file (`{"pending":N,"at":EPOCH}`). Missing or unreadable
//! means zero. It is committed right after the first refresh outcome, before any page is read:
//! a run killed earlier neither counts nor resets. Any outcome that is not pending resets it.
use crate::feeder::Env;
use crate::merge_close::{
    scheduled_pass, Pass, Refresh, ScheduleTiming, CONTINUE_WINDOW_SECS, PASS_BUDGET,
    PENDING_LIMIT, SCHEDULED_REFRESH_WAIT, WAIT_SWITCH,
};
use serde_json::json;
use std::cell::Cell;
use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// The pending count and the epoch second of the pass that last ended pending.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Streak {
    pub pending: u32,
    pub at: u64,
}

pub fn read_streak(path: &Path) -> Streak {
    let parsed = std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok());
    let Some(value) = parsed else {
        return Streak::default();
    };
    Streak {
        pending: value["pending"].as_u64().unwrap_or(0).min(u32::MAX.into()) as u32,
        at: value["at"].as_u64().unwrap_or(0),
    }
}

fn write_streak(path: &Path, streak: Streak) -> Result<(), String> {
    let text = json!({"pending": streak.pending, "at": streak.at}).to_string();
    pg_worker_core::run::record_io::atomic_write_private(path, text.as_bytes())
        .map_err(|err| format!("write merge-close state {}: {err}", path.display()))
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

/// The refresh wait: `PG_MERGE_CLOSE_REFRESH_WAIT_SECS` (whole seconds) or the default.
fn refresh_wait(env: &Env) -> Result<Duration, String> {
    match env.get(WAIT_SWITCH).map(|v| v.trim()) {
        None | Some("") => Ok(SCHEDULED_REFRESH_WAIT),
        Some(v) => v
            .parse::<u64>()
            .map(Duration::from_secs)
            .map_err(|_| format!("{WAIT_SWITCH} must be whole seconds")),
    }
}

/// One scheduled run; returns the exit status. `continue_only` (the continuation timer) does
/// nothing unless a pending pass left a scan that can still be read inside the index age.
pub fn run_unit(env: &Env, state: &Path, continue_only: bool, project: Option<&str>) -> i32 {
    let streak = read_streak(state);
    let started = now_secs();
    if continue_only
        && (streak.pending == 0 || started.saturating_sub(streak.at) > CONTINUE_WINDOW_SECS)
    {
        return 0;
    }
    let wait = match refresh_wait(env) {
        Ok(wait) => wait,
        Err(reason) => {
            eprintln!("merge-close: {reason}");
            return 1;
        }
    };
    let mut timing = ScheduleTiming::new(wait, PASS_BUDGET);
    if streak.pending > 0 {
        timing = timing.resuming();
    }
    let committed = Cell::new(false);
    let count = Cell::new(0u32);
    let mut on_refresh = |refresh: &Refresh| -> Result<(), String> {
        committed.set(true);
        match refresh {
            Refresh::Ready if streak.pending > 0 => write_streak(state, Streak::default()),
            Refresh::Ready => Ok(()),
            Refresh::Pending { .. } => {
                count.set(streak.pending.saturating_add(1));
                write_streak(
                    state,
                    Streak {
                        pending: count.get(),
                        at: started,
                    },
                )
            }
        }
    };
    let result = scheduled_pass(env, true, project, false, timing, &mut on_refresh);
    if !committed.get() && streak.pending > 0 {
        // The refresh itself failed: not pending, so the streak starts over.
        if let Err(reason) = write_streak(state, Streak::default()) {
            eprintln!("merge-close: {reason}");
        }
    }
    match result {
        Ok(Pass::Done(done)) => {
            print!("{}", done.text);
            0
        }
        Ok(Pass::Pending { waited_secs }) => {
            let pending = count.get();
            println!("refresh_pending\t{waited_secs}\t{pending}/{PENDING_LIMIT}");
            if pending >= PENDING_LIMIT {
                eprintln!("merge-close: merge index refresh pending in {pending} runs in a row");
                1
            } else {
                0
            }
        }
        Err(reason) => {
            eprintln!("merge-close: {reason}");
            1
        }
    }
}
