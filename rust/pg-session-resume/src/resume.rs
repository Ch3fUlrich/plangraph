//! Resume a stopped session on its findings file. A round counts as delivered only when
//! the runtime acknowledges it (`settle_claim`); a claim is not a delivery.

use crate::agent::resume_cmd;
use crate::contract::{list_findings, reviewed_head};
use crate::engine::{
    age, head_of_status, one_line, refuse_once, rounds_for_head, save, write_lane_resume, Config,
    MAX_PROMPT_BYTES,
};
use crate::host::Host;
use crate::record::{load_all, Record};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// How long a claim without a process may wait for the runtime's acknowledgement.
pub const CLAIM_GRACE_S: u64 = 120;

const CLAIM_KEYS: [&str; 6] = [
    "claim_round",
    "claim_ts",
    "claim_offset",
    "claim_mark",
    "claim_pid",
    "claim_start",
];

fn clear_claim(rec: &mut Record) {
    for key in CLAIM_KEYS {
        rec.set_resume(key, Value::Null);
    }
}

/// Only the newest stopped record of a lane may resume, and only while no record of
/// the lane is live: two records for one lane must not both start on one file.
fn eligible(all: &[Record], rec: &Record, lane: &str) -> bool {
    let key = |r: &Record| (r.resume_u64("stopped_ts").unwrap_or(0), r.label());
    let same = |r: &&Record| r.path != rec.path && r.lane().as_deref() == Some(lane);
    !all.iter()
        .filter(same)
        .any(|o| !o.stopped() || (o.resume().is_some() && key(o) > key(rec)))
}

/// A stopped writer with an unacknowledged findings round for the head it was stopped at
/// resumes in its own worktree; the prompt is exactly the file text.
pub fn resume_findings(host: &mut dyn Host, cfg: &Config) {
    let all = load_all(&cfg.launch_dir);
    for mut rec in load_all(&cfg.launch_dir) {
        let Some(lane) = rec.lane() else { continue };
        if rec.resume_u64("claim_round").is_some() {
            settle_claim(host, cfg, &mut rec, &lane);
        }
        if !rec.stopped() || rec.resume_u64("claim_round").is_some() || !eligible(&all, &rec, &lane)
        {
            continue;
        }
        let Some(handle) = rec.resume_str("handle").map(str::to_owned) else {
            continue;
        };
        let after = rec.resume_round();
        let Some(newest) = list_findings(&cfg.pr_dir, &lane)
            .into_iter()
            .rfind(|f| f.round > after)
        else {
            continue;
        };
        let name = rec.label();
        let refuse = |host: &mut dyn Host, rec: &mut Record, why: &str| {
            let line = format!(
                "event=resume-refused name={name} lane={lane} round={} why={why}",
                newest.round
            );
            refuse_once(host, rec, "resume_refused", &line, why);
        };
        let stopped_at = rec
            .resume_str("status_head")
            .or_else(|| rec.resume_str("head"))
            .map(str::to_owned);
        // The round names the head it reviewed (sidecar); it must be the head this session was
        // stopped at AND the head the lane is at now. The prompt text is never changed.
        let Some(stopped_at) = stopped_at else {
            refuse(host, &mut rec, "no-stop-head");
            continue;
        };
        let Some(found) = rounds_for_head(cfg, &lane, after, Some(&stopped_at)).pop() else {
            let why = if reviewed_head(&cfg.pr_dir, &lane, newest.round).is_none() {
                "no-reviewed-head"
            } else {
                "head-mismatch"
            };
            refuse(host, &mut rec, why);
            continue;
        };
        // A verified current head is required: a missing or unreadable status proves nothing.
        match head_of_status(cfg, &lane) {
            Some(now) if now == stopped_at => {}
            Some(_) => {
                refuse(host, &mut rec, "head-superseded");
                continue;
            }
            None => {
                refuse(host, &mut rec, "no-lane-head");
                continue;
            }
        }
        resume_one(
            host,
            cfg,
            &mut rec,
            &lane,
            &handle,
            found.round,
            &found.path,
        );
    }
}

/// The runtime's answer for a claim left in the record. Delivered: the transcript holds the
/// prompt after the claim's offset (or, for a runtime without a transcript, the process this
/// launch started is alive with its recorded start time). Not delivered and nothing running
/// (or the grace is over): the claim is dropped and the session is stopped again, so the same
/// round is sent again.
fn settle_claim(host: &mut dyn Host, cfg: &Config, rec: &mut Record, lane: &str) {
    let (Some(round), name) = (rec.resume_u64("claim_round"), rec.label()) else {
        return;
    };
    let claimed_at = rec.resume_u64("claim_ts").unwrap_or(0);
    // A tick that died between the spawn and saving the pid left the launch without an
    // identity: find the process again from its command line first, so an acknowledgement
    // carries it and a retry never starts a second writer beside the first.
    if rec.resume_u64("claim_pid").is_none() && rec.agent() != crate::record::Agent::Claude {
        let handle = rec.resume_str("handle").unwrap_or_default().to_owned();
        if let Some((pid, start)) = host.find_process(&handle) {
            rec.set_resume("claim_pid", json!(pid));
            rec.set_resume("claim_start", json!(start));
            save(host, rec);
        }
    }
    let proc = rec
        .resume_u64("claim_pid")
        .zip(rec.resume_u64("claim_start"));
    let alive =
        proc.is_some_and(|(p, s)| u32::try_from(p).is_ok_and(|p| host.proc_start(p) == Some(s)));
    let prompt = list_findings(&cfg.pr_dir, lane)
        .into_iter()
        .find(|f| f.round == round)
        .and_then(|f| std::fs::read_to_string(f.path).ok());
    let acked = match &prompt {
        Some(text) => host
            .transcript_contains(rec, rec.resume_u64("claim_offset").unwrap_or(0), text)
            .unwrap_or_else(|| {
                // No transcript: the process this launch started must carry the prompt AND the
                // session must have shown activity of its own since the claim; a live process
                // that never read its arguments writes nothing.
                let moved = match (host.activity_mark(rec), rec.resume_u64("claim_mark")) {
                    (Some(now), Some(then)) => now > then,
                    (Some(_), None) => true,
                    _ => false,
                };
                moved
                    && proc.is_some_and(|(p, s)| {
                        u32::try_from(p).is_ok_and(|p| host.process_holds_prompt(p, s, text))
                    })
            }),
        None => false,
    };
    if acked {
        let handle = rec.resume_str("handle").unwrap_or_default().to_owned();
        // The verified launch identity stays in the record even when the process has already
        // exited: a later stop sees "gone" and records the session, instead of finding nothing.
        let found = proc
            .and_then(|(p, s)| Some((u32::try_from(p).ok()?, s)))
            .or_else(|| {
                host.find_process(&handle)
                    .filter(|_| rec.agent() != crate::record::Agent::Claude)
            });
        rec.set("pid", found.map_or(Value::Null, |(p, _)| json!(p)));
        rec.set("pid_start", found.map_or(Value::Null, |(_, s)| json!(s)));
        rec.set("state", json!("running"));
        rec.set("stopped_ts", Value::Null);
        rec.set("resume_refused", Value::Null);
        rec.set_resume("round", json!(round));
        rec.set_resume("resumed_ts", json!(host.now()));
        clear_claim(rec);
        save(host, rec);
        write_lane_resume(host, cfg, rec, lane, "running");
        host.log(&format!(
            "event=resume-acked name={name} lane={lane} round={round}"
        ));
        return;
    }
    let waiting =
        alive || (proc.is_none() && host.now().saturating_sub(claimed_at) < CLAIM_GRACE_S);
    if waiting && prompt.is_some() {
        return;
    }
    let stopped_ts = rec
        .resume_u64("stopped_ts")
        .map_or(Value::Null, |t| json!(t));
    rec.set("state", json!("stopped"));
    rec.set("stopped_ts", stopped_ts);
    clear_claim(rec);
    save(host, rec);
    host.log(&format!(
        "event=resume-reconciled name={name} lane={lane} round={round} outcome=not-acked"
    ));
}

fn worktree_label(host: &dyn Host, rec: &Record, cwd: &Path) -> String {
    let mut parts = vec![match host.git_dirty(cwd) {
        Some(true) => "dirty",
        Some(false) => "clean",
        None => "unknown",
    }];
    match (host.git_head(cwd), rec.resume_str("head")) {
        (Some(n), Some(t)) if n != t => parts.push("moved"),
        (Some(_), Some(_)) => {}
        _ => parts.push("head-unknown"),
    }
    match (host.worktree_id(cwd), rec.resume_str("worktree")) {
        (Some(n), Some(t)) if n != t => parts.push("replaced"),
        (Some(_), Some(_)) => {}
        _ => parts.push("worktree-unknown"),
    }
    parts.join("+")
}

fn resume_one(
    host: &mut dyn Host,
    cfg: &Config,
    rec: &mut Record,
    lane: &str,
    handle: &str,
    round: u64,
    file: &Path,
) {
    let name = rec.label();
    let size = std::fs::metadata(file).map(|m| m.len()).unwrap_or(0);
    let prompt = match std::fs::read_to_string(file) {
        Ok(p) if !p.trim().is_empty() && size <= MAX_PROMPT_BYTES => p,
        Ok(p) if !p.trim().is_empty() => {
            rec.set_resume("round", json!(round)); // refused for good: it can never be sent
            save(host, rec);
            host.log(&format!("event=resume-refused name={name} lane={lane} round={round} bytes={size} why=too-large"));
            return;
        }
        _ => return, // empty or unreadable: not a findings file yet (written atomically by contract)
    };
    let Some(cwd) = rec.str("cwd").map(PathBuf::from).filter(|c| c.is_dir()) else {
        let line =
            format!("event=resume-refused name={name} lane={lane} round={round} why=no-worktree");
        refuse_once(host, rec, "resume_refused", &line, "no-worktree");
        return;
    };
    let free = host.mem_available_mb();
    if free.is_some_and(|f| f < cfg.min_free_mb) {
        host.log(&format!(
            "event=resume-deferred name={name} lane={lane} round={round} why=low-ram free_mb={}",
            free.unwrap_or(0)
        ));
        return;
    }
    let worktree = worktree_label(host, rec, &cwd);
    // Claim the round before the launch. A claim is not a delivery: whichever tick sees it
    // next asks the runtime (settle_claim) whether the prompt arrived.
    rec.set_resume("claim_round", json!(round));
    rec.set_resume("claim_ts", json!(host.now()));
    rec.set_resume(
        "claim_offset",
        host.transcript_len(rec).map_or(Value::Null, |n| json!(n)),
    );
    rec.set_resume(
        "claim_mark",
        host.activity_mark(rec).map_or(Value::Null, |n| json!(n)),
    );
    if let Err(e) = rec.save() {
        host.log(&format!(
            "event=resume-failed name={name} lane={lane} round={round} error=claim not saved: {e}"
        ));
        return;
    }
    let cmd = resume_cmd(rec.agent(), handle, &prompt);
    let started = if cmd.detached {
        host.spawn(&cwd, &cmd.argv).map(Some)
    } else {
        host.run(Some(&cwd), &cmd.argv).map(|_| None)
    };
    let pid = match started {
        Ok(pid) => pid,
        Err(e) => {
            clear_claim(rec);
            save(host, rec);
            host.log(&format!(
                "event=resume-failed name={name} lane={lane} round={round} error={}",
                one_line(&e)
            ));
            return;
        }
    };
    let start = pid.and_then(|p| host.launch_start(p));
    rec.set_resume(
        "claim_pid",
        pid.filter(|_| start.is_some())
            .map_or(Value::Null, |p| json!(p)),
    );
    rec.set_resume("claim_start", start.map_or(Value::Null, |s| json!(s)));
    // The session runs now; whether it got the prompt is settled below and on later ticks.
    rec.set("state", json!("running"));
    rec.set("stopped_ts", Value::Null);
    save(host, rec);
    let stopped_for = host
        .now()
        .saturating_sub(rec.resume_u64("stopped_ts").unwrap_or(0));
    let (age_s, free) = (
        age(host, rec),
        free.map_or("unknown".to_owned(), |f| f.to_string()),
    );
    host.log(&format!(
        "event=resume name={name} age_s={age_s} reason=findings lane={lane} round={round} stopped_for_s={stopped_for} ram_freed_mb=0 ram_free_mb={free} worktree={worktree} handle={handle}"
    ));
    settle_claim(host, cfg, rec, lane);
}
