//! `plangraph wait files`: wait for writer status files to end and seat result files to appear.
use crate::wait::{Options, Outcome, DEFAULT_DEADLINE, DEFAULT_POLL};
use std::path::PathBuf;
use std::time::{Duration, Instant};

/// State of a writer status file: `Done` at its `<NAME> DONE` line, `Blocked` at a `BLOCKED` line.
#[derive(Debug, PartialEq)]
pub enum FileState {
    Pending,
    Done,
    Blocked,
}

pub fn status_state(text: &str) -> FileState {
    for line in text.lines() {
        if line.starts_with("BLOCKED") {
            return FileState::Blocked;
        }
    }
    if text
        .lines()
        .any(|line| line.ends_with(" DONE") && !line.starts_with("DONE "))
    {
        FileState::Done
    } else {
        FileState::Pending
    }
}

/// Wait for every status file to end (`NAME DONE` or `BLOCKED`) and every result file to exist.
pub fn wait_files(
    statuses: &[PathBuf],
    results: &[PathBuf],
    sleep: &mut dyn FnMut(Duration),
    now: &mut dyn FnMut() -> Duration,
    deadline: Duration,
    poll: Duration,
) -> Outcome {
    loop {
        let mut pending = Vec::new();
        let mut blocked = Vec::new();
        for path in statuses {
            match status_state(&std::fs::read_to_string(path).unwrap_or_default()) {
                FileState::Done => {}
                FileState::Blocked => blocked.push(path.display().to_string()),
                FileState::Pending => pending.push(path.display().to_string()),
            }
        }
        for path in results {
            if std::fs::metadata(path).map_or(true, |meta| meta.len() == 0) {
                pending.push(path.display().to_string());
            }
        }
        if pending.is_empty() {
            return if blocked.is_empty() {
                Outcome::Green {
                    detail: format!(
                        "{} status, {} result files ended",
                        statuses.len(),
                        results.len()
                    ),
                }
            } else {
                Outcome::Red {
                    detail: format!("blocked: {}", blocked.join(" ")),
                }
            };
        }
        // The files are read once more at the deadline itself: a poll longer than the time left
        // sleeps only the remainder, so a writer finishing inside the deadline is not missed.
        let remaining = deadline.saturating_sub(now());
        if remaining.is_zero() {
            return Outcome::Timeout {
                detail: format!(
                    "deadline {}s: still pending: {}",
                    deadline.as_secs(),
                    pending.join(" ")
                ),
            };
        }
        sleep(poll.min(remaining));
    }
}

pub(crate) fn run_files(options: &Options) -> Result<(Outcome, String, Duration), String> {
    if options.statuses.is_empty() && options.results.is_empty() {
        return Err("wait files needs at least one --status or --result".into());
    }
    let start = Instant::now();
    let outcome = wait_files(
        &options.statuses,
        &options.results,
        &mut std::thread::sleep,
        &mut || start.elapsed(),
        options.deadline.unwrap_or(DEFAULT_DEADLINE),
        options.poll.unwrap_or(DEFAULT_POLL),
    );
    let first = options
        .statuses
        .iter()
        .chain(&options.results)
        .next()
        .and_then(|p| p.file_name());
    Ok((
        outcome,
        format!(
            "files-{}",
            first.map_or_else(String::new, |n| n.to_string_lossy().into_owned())
        ),
        start.elapsed(),
    ))
}

#[cfg(test)]
#[path = "wait_files_tests.rs"]
mod tests;
