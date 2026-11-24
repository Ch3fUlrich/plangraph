//! Native writer admission. Verdict snapshots are immutable once observed.
use crate::{errors::ToolError, placement::Args};
use pg_worker_core::{lockfile::LockGuard, run::record::run_dir};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{io::Read, path::Path};

#[cfg(test)]
#[path = "round_progress_tests.rs"]
mod tests;

pub(crate) fn refused(reason: &str) -> ToolError {
    crate::placement::parameter(format!("{reason}: writer round admission refused"))
}
/// The one owner of the writer review-round stop rule (the Python setting, the batch-writer
/// skill and the batch card quote it): the loop ends when `STALL_ROUNDS` rounds in a row bring
/// the HIGH+MED count no lower than the best count so far, when from round `STALL_ROUNDS`
/// on the count is still above half of the round-1 count, and never runs past `MAX_ROUNDS`.
pub(crate) const MAX_ROUNDS: u64 = 8;
pub(crate) const STALL_ROUNDS: u64 = 3;

/// Why the next writer round (`next`) must not start, given the confirmed HIGH+MED count of
/// every round so far.
pub(crate) fn stop_reason(counts: &[u64], next: u64, used_exception: bool) -> Option<&'static str> {
    if next > MAX_ROUNDS || used_exception {
        return Some("round_cap");
    }
    stall_reason(counts, 0)
}

/// The stall of the rule on `counts`, where the streak of misses starts at round index `since`
/// (a level the loop was moved to a stronger leg at; earlier rounds only set the best count) and
/// the half-of-round-1 check applies to the first level only.
pub(crate) fn stall_reason(counts: &[u64], since: usize) -> Option<&'static str> {
    // Round 1 has no earlier count to beat: it is a miss until a later round is strictly lower.
    let (mut best, mut misses) = (None, 0);
    for (index, &count) in counts.iter().enumerate() {
        match best {
            Some(b) if count < b => (best, misses) = (Some(count), 0),
            _ => {
                best = best.or(Some(count));
                misses += 1;
            }
        }
        if index < since {
            misses = 0;
        }
        if misses >= STALL_ROUNDS {
            return Some("no_progress");
        }
    }
    match (counts.first(), counts.last()) {
        (Some(&first), Some(&last))
            if since == 0 && counts.len() as u64 >= STALL_ROUNDS && last * 2 > first =>
        {
            Some("slow_progress")
        }
        _ => None,
    }
}
pub(crate) fn summarize(
    value: &Value,
    run: &str,
    item: &str,
    pr: &Value,
) -> Result<Value, ToolError> {
    let invalid = || refused("round_verdict_invalid");
    if value["writer_run_id"] != run || value["item_key"] != item || &value["pr"] != pr {
        return Err(invalid());
    }
    if !matches!(
        value["verdict"].as_str(),
        Some("ACCEPT" | "REQUEST_CHANGES")
    ) {
        return Err(invalid());
    }
    let findings = value["findings"].as_array().ok_or_else(invalid)?;
    let (mut high, mut med, mut clear) = (0, 0, true);
    for finding in findings {
        let severity = finding["severity"].as_str().ok_or_else(invalid)?;
        let confirmed = finding["confirmed"].as_bool().ok_or_else(invalid)?;
        if !matches!(severity, "HIGH" | "MED" | "LOW")
            || finding["text"].as_str().is_none_or(|s| s.trim().is_empty())
            || finding.get("clear_fix").is_some_and(|v| !v.is_boolean())
        {
            return Err(invalid());
        }
        if confirmed {
            match severity {
                "HIGH" => high += 1,
                "MED" => {
                    med += 1;
                    clear &= finding["clear_fix"] == true;
                }
                _ => {}
            }
        }
    }
    if value["verdict"] == "ACCEPT" && high + med != 0 {
        return Err(invalid());
    }
    Ok(json!({"confirmed_blockers":high + med,
        "narrow_fix_eligible":high == 0 && (1..=2).contains(&med) && clear,
        "verdict":value}))
}
pub(crate) fn read_json(path: &Path) -> Result<Value, ToolError> {
    let read = || -> std::io::Result<Value> {
        // Reuse the record reader's no-follow, regular-file checks, including FIFOs.
        let file = crate::records::open_record(path)?;
        let mut bytes = Vec::new();
        file.take(1024 * 1024 + 1).read_to_end(&mut bytes)?;
        if bytes.len() > 1024 * 1024 {
            return Err(std::io::Error::other("verdict too large"));
        }
        Ok(serde_json::from_slice(&bytes)?)
    };
    read().map_err(|_| refused("round_verdict_invalid"))
}

/// The writer rounds of `item` that count toward the stop rule (voided and refused attempts
/// excluded), ordered by round.
pub(crate) fn round_history(state: &Path, item: &str) -> Result<Vec<Value>, ToolError> {
    let mut history = Vec::new();
    match std::fs::read_dir(state.join("runs")) {
        Ok(entries) => {
            for entry in entries {
                let entry = entry.map_err(|_| refused("round_history_unavailable"))?;
                let name = entry.file_name();
                let Some(id) = name.to_str().filter(|s| s.starts_with("run-")) else {
                    continue;
                };
                if let Err(error) = std::fs::symlink_metadata(entry.path().join("record.json")) {
                    if error.kind() == std::io::ErrorKind::NotFound {
                        continue; // Prompt storage precedes creation of the run record.
                    }
                    return Err(refused("round_history_unavailable"));
                }
                let record = crate::records::read_record(state, &json!(id))
                    .map_err(|_| refused("round_history_unavailable"))?;
                if record["role"] == "implement"
                    && crate::round_escalate::batch_key(&record) == Some(item)
                    && record["writer_round"].is_u64()
                    && !(record["round_void"] == true
                        && crate::runs::terminal(&record)
                        && record["status"] != "completed")
                    && !matches!(record["status"].as_str(), Some("dry_run" | "refused"))
                {
                    history.push(record);
                }
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err(refused("round_history_unavailable")),
    }
    history.sort_by_key(|r| r["writer_round"].as_u64().unwrap());
    Ok(history)
}

pub(crate) struct Admission {
    // Held until the run record (or refusal) has been persisted, never across wait().
    _lock: Option<LockGuard>,
    pub fields: Value,
    /// The delivered replan of a stalled loop, appended to the prompt of the resumed writer.
    pub prompt_addendum: Option<String>,
}
/// Admit the next writer round. A stall of the rule queues a replan record and still admits the
/// round on the same writer; only the ceiling, an accepted verdict or the spent narrow exception
/// refuse.
pub(crate) fn admit(state: &Path, args: &Args) -> Result<Admission, ToolError> {
    let untracked = || Admission {
        _lock: None,
        fields: json!({}),
        prompt_addendum: None,
    };
    if args.get("role").is_some_and(|v| v != "implement") {
        return Ok(untracked());
    }
    let mut identity = json!({"role":"implement"});
    crate::records::annotate_item(&mut identity, &args["card"], &mut vec![]);
    let Some(item) = identity["item_key"].as_str() else {
        return Ok(untracked());
    };
    let card = &args["card"];
    let pr = card.get("pr").cloned().unwrap_or(Value::Null);
    if !pr.is_null() && pr.as_str().is_none_or(|s| s.trim().is_empty()) {
        return Err(refused("round_pr_invalid"));
    }
    let path = card
        .get("round_verdict_file")
        .cloned()
        .unwrap_or(Value::Null);
    if !path.is_null() && path.as_str().is_none_or(|s| !Path::new(s).is_absolute()) {
        return Err(refused("round_verdict_invalid"));
    }
    if card.get("narrow_fix").is_some_and(|v| !v.is_boolean()) {
        return Err(refused("round_exception_invalid"));
    }
    // The ledger key: one stable batch key when the card names it, else the item key.
    let key = match card.get("batch_key").filter(|v| !v.is_null()) {
        None => item.to_string(),
        Some(v) => v
            .as_str()
            .filter(|s| crate::records::valid_item(s))
            .ok_or_else(|| refused("round_batch_key_invalid"))?
            .to_string(),
    };
    let item = key.as_str();
    let cause = crate::round_escalate::cause(card)?;
    let checks = crate::round_escalate::failing_checks(card)?;
    if cause != crate::round_escalate::CAUSES[0]
        && cause != crate::round_escalate::INITIAL
        && checks.is_none()
    {
        // A check-triggered round must show its failure evidence, else a red round reads as green.
        return Err(refused("round_failing_checks_missing"));
    }
    std::fs::create_dir_all(state).map_err(|_| refused("round_history_unavailable"))?;
    let lock_name = format!("writer-rounds-{:x}.lock", Sha256::digest(item.as_bytes()));
    let lock = pg_worker_core::lockfile::try_lock(&state.join(lock_name))
        .map_err(|_| refused("round_history_unavailable"))?
        .ok_or_else(|| refused("round_admission_busy"))?;
    let history = round_history(state, item)?;
    if cause == crate::round_escalate::INITIAL {
        // The first implementation is no fix turn: it takes no round number.
        if !history.is_empty() {
            return Err(refused("round_initial_after_fix"));
        }
        return Ok(Admission {
            _lock: Some(lock),
            fields: json!({"round_cause":cause, "batch_key":item}),
            prompt_addendum: None,
        });
    }
    let next = history.len() as u64 + 1;
    let used_exception = history.iter().any(|r| r["narrow_fix"] == true);
    let mut summaries = vec![];
    // The failing checks seen when round `index + 2` (or this one) starts follow round `index + 1`.
    let after = |index: usize| -> Option<u64> {
        history
            .get(index + 1)
            .map_or(checks, |r| r["round_failing_checks"].as_u64())
    };
    for (index, record) in history.iter().enumerate() {
        if record["writer_round"] != index as u64 + 1 || record["round_pr"] != pr {
            return Err(refused("round_history_invalid"));
        }
        if !crate::runs::terminal(record) {
            return Err(refused("round_in_flight"));
        }
        let run = record["run_id"].as_str().unwrap();
        let snapshot = run_dir(state, run)
            .map_err(|_| refused("round_history_invalid"))?
            .join("round-verdict.json");
        let summary = if snapshot.exists() {
            let saved = read_json(&snapshot)?;
            let checked = summarize(&saved["verdict"], run, item, &pr)?;
            if saved != checked {
                return Err(refused("round_history_invalid"));
            }
            saved
        } else if record["round_verdict_file"].is_null()
            && record["round_cause"]
                .as_str()
                .is_some_and(|c| c != crate::round_escalate::CAUSES[0])
        {
            // A round judged by its failing checks alone (red CI, fast check, rebase); an unknown
            // count after it is green. A review round always needs its verdict.
            json!({"confirmed_blockers":0, "narrow_fix_eligible":false, "verdict":null})
        } else {
            let source = record["round_verdict_file"]
                .as_str()
                .ok_or_else(|| refused("round_verdict_missing"))?;
            let summary = summarize(&read_json(Path::new(source))?, run, item, &pr)?;
            pg_worker_core::run::record_io::atomic_write_private(
                &snapshot,
                summary.to_string().as_bytes(),
            )
            .map_err(|_| refused("round_history_unavailable"))?;
            summary
        };
        summaries.push(summary);
    }
    // A card that omits the check count carries the last observed count forward: unknown
    // evidence never credits progress, however many cards in a row omit it.
    let mut observed = 0;
    let counts: Vec<_> = summaries
        .iter()
        .enumerate()
        .map(|(i, s)| {
            observed = after(i)
                .or_else(|| history[i]["round_failing_checks"].as_u64())
                .unwrap_or(observed);
            s["confirmed_blockers"].as_u64().unwrap() + observed
        })
        .collect();
    if next > MAX_ROUNDS {
        return Err(refused("round_cap"));
    }
    // An accepted review ends the review loop only; red checks or a moved main still get a
    // counted fix round.
    if cause == crate::round_escalate::CAUSES[0]
        && summaries
            .last()
            .is_some_and(|s| s["verdict"]["verdict"] == "ACCEPT")
    {
        return Err(refused("round_accepted"));
    }
    if used_exception {
        return Err(refused("round_cap"));
    }
    let (level, since) = crate::round_escalate::level(&history);
    let reason = stall_reason(&counts, since);
    let narrow = card["narrow_fix"] == true;
    if narrow
        && (reason.is_none()
            || checks.unwrap_or(0) != 0
            || !summaries
                .last()
                .is_some_and(|s| s["narrow_fix_eligible"] == true))
    {
        return Err(refused("round_exception_invalid"));
    }
    let (mut new_level, mut why, mut replan) = (level, Value::Null, Value::Null);
    let mut addendum = None;
    if let (Some(reason), false) = (reason, narrow) {
        // A stall needs a durable replan job, and the round launches only after that replan
        // planned its slices and reported; until then it is refused without using a round.
        let Some(pr) = pr.as_str() else {
            return Err(refused(reason));
        };
        new_level = level + 1;
        why = json!(reason);
        let dry = args.get("dry_run") == Some(&json!(true));
        if !crate::round_replan::escalation_sent(state, item, pr, new_level) {
            if !dry {
                crate::round_replan::enqueue_escalation(state, item, pr, new_level)
                    .map_err(|_| refused("round_replan_unavailable"))?;
            }
            return Err(refused("round_replan_pending"));
        }
        replan = json!("done");
        addendum = crate::round_replan::escalation_brief(state, item, pr, new_level);
    }
    Ok(Admission {
        _lock: Some(lock),
        fields: json!({"writer_round":next, "batch_key":item,
        "round_pr":pr, "round_verdict_file":path, "narrow_fix":narrow,
        "previous_confirmed_blockers":counts, "round_cause":cause, "round_failing_checks":checks,
        "round_escalation":new_level, "round_escalation_reason":why, "round_replan":replan}),
        prompt_addendum: addendum,
    })
}

pub(crate) fn record_refusal(
    state: &Path,
    host: &str,
    args: &Args,
    error: ToolError,
) -> Result<Value, ToolError> {
    let error = error.into_error_data();
    let reason = error
        .message
        .split(':')
        .next()
        .unwrap_or("round_history_invalid");
    let mut base = json!({"role":"implement", "reason":reason,
        "refusal":"SpawnRefused", "error":error.message, "exit_code":2});
    crate::records::annotate_item(&mut base, &args["card"], &mut vec![]);
    // A stop of the round rule starts the durable replan of the PR (round_replan).
    let dry = args.get("dry_run") == Some(&json!(true));
    let key = args["card"]["batch_key"]
        .as_str()
        .filter(|s| crate::records::valid_item(s))
        .or_else(|| base["item_key"].as_str())
        .map(String::from);
    if let (Some(item), None, false) = (key.as_deref(), args["card"]["pr"].as_str(), dry) {
        // No PR to replan: the batch still goes back, by one delivered routing line.
        if ["no_progress", "slow_progress", "round_cap"].contains(&reason) {
            let findings =
                crate::round_replan_ports::last_round_findings_of(state, item, &Value::Null)
                    .unwrap_or_default();
            match crate::round_replan::enqueue_return(state, item, reason, &findings) {
                Ok(true) => base["replan"] = json!("returned"),
                Ok(false) => {}
                Err(e) => base["replan_error"] = json!(e.into_error_data().message),
            }
        }
    }
    if let (Some(item), Some(pr), false) = (key.as_deref(), args["card"]["pr"].as_str(), dry) {
        match crate::round_replan::enqueue(state, item, pr, reason) {
            Ok(true) => base["replan"] = json!("queued"),
            Ok(false) => {}
            Err(e) => base["replan_error"] = json!(e.into_error_data().message),
        }
    }
    crate::runs::create(
        state,
        &pg_worker_core::run::record::new_run_id(),
        host,
        base,
        pg_worker_core::run::record::Status::Refused,
    )
    .map(|r| r.to_value())
}

pub(crate) fn work_fingerprint(cwd: &Path) -> Option<String> {
    use std::{process::Stdio, time::Duration};
    let git = |args: &[&str]| {
        #[cfg(test)]
        let mut cmd = pg_worker_core::git_test_support::command(cwd);
        #[cfg(not(test))]
        let mut cmd = std::process::Command::new("git");
        cmd.current_dir(cwd)
            .args(args)
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0);
        }
        crate::prompt_git::collect_diff(cmd.spawn().ok()?, 8 * 1024 * 1024, Duration::from_secs(5))
            .ok()
    };
    let mut hash = Sha256::new();
    let mut add = |bytes: &[u8]| {
        hash.update(bytes.len().to_le_bytes());
        hash.update(bytes);
    };
    add(&git(&["rev-parse", "HEAD"])?);
    add(&git(&[
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        "HEAD",
        "--",
    ])?);
    let files = git(&["ls-files", "--others", "--exclude-standard", "-z"])?;
    add(&files);
    let mut remaining = 8 * 1024 * 1024;
    for file in files.split(|b| *b == 0).filter(|s| !s.is_empty()) {
        let path = cwd.join(std::str::from_utf8(file).ok()?);
        let file = crate::records::open_record(&path).ok()?;
        let mut bytes = vec![];
        file.take(remaining + 1).read_to_end(&mut bytes).ok()?;
        remaining = remaining.checked_sub(bytes.len() as u64)?;
        add(&bytes);
    }
    Some(format!("{:x}", hash.finalize()))
}

pub(crate) fn finish_round(
    record: &mut pg_worker_core::run::record::RunRecord,
    status: pg_worker_core::run::record::Status,
) {
    use pg_worker_core::run::record::Status;
    if record.get("writer_round").is_none() || status == Status::Completed {
        return;
    }
    let before = record.get("round_work_before").and_then(Value::as_str);
    let unchanged = before.is_some_and(|before| {
        record
            .get("cwd")
            .and_then(Value::as_str)
            .and_then(|cwd| work_fingerprint(Path::new(cwd)))
            .as_deref()
            == Some(before)
    });
    let guarded = record
        .get("reason")
        .and_then(Value::as_str)
        .is_some_and(|s| s.starts_with("gitconfig_"));
    record.set(
        "round_void",
        json!(
            status.is_terminal()
                && status != Status::Completed
                && !guarded
                && (record.get("pid").is_none_or(Value::is_null) || unchanged)
        ),
    );
}
