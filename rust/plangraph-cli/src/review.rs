//! `plangraph review` command: check freshness, read writer families, split oversize diffs,
//! spawn review seats via the spawner route, parse verdicts/test-gaming, and write land approvals.
use crate::atomic;
use crate::base_fresh::python_command;
use crate::review_git::{
    measure_diff_files, resolve_and_check_fresh, writer_groups, ResolvedRange, WriterGroup,
};
use pg_worker_core::review_split::{
    group_banner, plan_groups_from_measured, SeatVerdict, TestGaming,
};
use pg_worker_core::run::record::run_dir;
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

#[path = "review_seat_completion.rs"]
mod completion;
#[path = "review_outcome.rs"]
mod outcome;
use outcome::evaluate_seat_record;

#[derive(Debug, Clone)]
pub struct ReviewArgs {
    pub repo: PathBuf,
    pub head: String,
    pub base: String,
    pub item: Option<String>,
    pub focus: Option<PathBuf>,
    pub approval: Option<PathBuf>,
    pub state: Option<PathBuf>,
    pub wait_s: u64,
    pub no_fetch: bool,
    pub dry_run: bool,
    pub seats: bool,
}

#[derive(Debug, Clone)]
pub struct SeatOutcome {
    pub index: usize,
    pub total: usize,
    pub run_id: String,
    pub status: String,
    pub exit_code: i64,
    pub leg: String,
    pub family: String,
    pub harness: String,
    pub files_read: Option<String>,
    pub verdict: Option<SeatVerdict>,
    pub test_gaming: TestGaming,
    pub paths_summary: String,
    pub passed: bool,
}

pub fn usage() -> &'static str {
    "usage: plangraph review --repo <path> --head <sha> [--base origin/main] [--item <key>] [--focus <file>] [--approval <path>] [--state <path>] [--wait <secs>] [--no-fetch] [--dry-run] [--seats]"
}

pub fn parse_args(args: &[String]) -> Result<Option<ReviewArgs>, String> {
    if args.is_empty() || args.iter().any(|a| a == "-h" || a == "--help") {
        return Ok(None);
    }
    let mut repo: Option<PathBuf> = None;
    let mut head: Option<String> = None;
    let mut base = "origin/main".to_owned();
    let mut item: Option<String> = None;
    let mut focus: Option<PathBuf> = None;
    let mut approval: Option<PathBuf> = None;
    let mut state: Option<PathBuf> = None;
    let mut wait_s: u64 = 2700;
    let mut no_fetch = false;
    let mut dry_run = false;
    let mut seats = false;

    let mut i = 0;
    while i < args.len() {
        let flag = args[i].as_str();
        let next_val = |idx: &mut usize, name: &str| -> Result<String, String> {
            *idx += 1;
            args.get(*idx)
                .cloned()
                .ok_or_else(|| format!("missing value for {name}"))
        };
        match flag {
            "--repo" => repo = Some(PathBuf::from(next_val(&mut i, flag)?)),
            "--head" => head = Some(next_val(&mut i, flag)?),
            "--base" => base = next_val(&mut i, flag)?,
            "--item" => item = Some(next_val(&mut i, flag)?),
            "--focus" => focus = Some(PathBuf::from(next_val(&mut i, flag)?)),
            "--approval" => approval = Some(PathBuf::from(next_val(&mut i, flag)?)),
            "--state" => state = Some(PathBuf::from(next_val(&mut i, flag)?)),
            "--wait" => {
                let raw = next_val(&mut i, flag)?;
                wait_s = raw
                    .parse::<u64>()
                    .map_err(|_| format!("invalid --wait value: {raw}"))?;
            }
            "--no-fetch" => no_fetch = true,
            "--dry-run" => dry_run = true,
            "--seats" => seats = true,
            other => return Err(format!("unknown argument: {other}\n{}", usage())),
        }
        i += 1;
    }

    let head = head.ok_or_else(|| format!("--head is required\n{}", usage()))?;
    let repo = repo.unwrap_or_else(|| PathBuf::from("."));
    Ok(Some(ReviewArgs {
        repo,
        head,
        base,
        item,
        focus,
        approval,
        state,
        wait_s,
        no_fetch,
        dry_run,
        seats,
    }))
}

fn spawner_command(repo_root: &Path) -> Command {
    if let Ok(custom) = std::env::var("PLANGRAPH_SPAWNER_CMD") {
        let mut parts = custom.split_whitespace();
        if let Some(bin) = parts.next() {
            let mut cmd = Command::new(bin);
            for arg in parts {
                cmd.arg(arg);
            }
            return cmd;
        }
    }
    let mut cmd = python_command(Some(repo_root));
    cmd.args(["-m", "plangraph.spawner.cli"]);
    cmd
}

fn resolve_state_dir(cli_state: Option<&Path>) -> Option<PathBuf> {
    if let Some(p) = cli_state {
        return Some(p.to_path_buf());
    }
    if let Ok(p) = std::env::var("PLANGRAPH_STATE_DIR") {
        if !p.trim().is_empty() {
            return Some(PathBuf::from(p));
        }
    }
    if let Ok(xdg) = std::env::var("XDG_STATE_HOME") {
        if !xdg.trim().is_empty() {
            return Some(PathBuf::from(xdg).join("plangraph"));
        }
    }
    std::env::var("HOME")
        .ok()
        .filter(|h| !h.trim().is_empty())
        .map(|h| PathBuf::from(h).join(".local/state/plangraph"))
}

fn make_card(
    repo_root: &Path,
    base_sha: &str,
    head_sha: &str,
    item: Option<&str>,
    paths: Option<&[String]>,
) -> Value {
    let short_head = &head_sha[..head_sha.len().min(8)];
    let mut diff_ref = json!({
        "repo": repo_root.display().to_string(),
        "base": base_sha,
        "head": head_sha,
    });
    if let Some(p) = paths {
        diff_ref["paths"] = json!(p);
    }
    let mut card = json!({
        "id": item.map(str::to_owned).unwrap_or_else(|| format!("REVIEW-{short_head}")),
        "title": format!("Review {short_head}"),
        "estimate_tokens": 20000,
        "diff_ref": diff_ref,
    });
    if let Some(key) = item {
        card["item"] = json!(key);
    }
    card
}

fn build_base_prompt(
    base_sha: &str,
    head_sha: &str,
    item: Option<&str>,
    focus_text: Option<&str>,
    group: Option<&WriterGroup>,
) -> String {
    let mut lines = vec![
        format!("Review the changes in {base_sha}..{head_sha} carefully for correctness, regressions, and test integrity."),
    ];
    if let Some(group) = group {
        lines.push(format!(
            "You are the seat for the commits written by {}: {}. Your family differs from theirs; judge the whole range for regressions, and report the findings in these commits.",
            group.families.join("+"),
            group.commits.join(" ")
        ));
    }
    if let Some(key) = item {
        lines.push(format!("Plan item: {key}"));
    }
    if let Some(focus) = focus_text {
        let trimmed = focus.trim();
        if !trimmed.is_empty() {
            lines.push(String::new());
            lines.push("Review focus and done criteria:".to_owned());
            lines.push(trimmed.to_owned());
        }
    }
    lines.push(String::new());
    lines.push("List the repository paths you opened on a FILES READ: line.".to_owned());
    lines.push(format!(
        "End your reply with these three exact trailing lines (substituting yes if tests are gamed and REQUEST_CHANGES if fixes are needed):\nHEAD: {head_sha}\nTEST-GAMING: no\nVERDICT: PASS"
    ));
    lines.join("\n")
}

fn run_route(
    repo_root: &Path,
    state_dir: Option<&Path>,
    card_path: &Path,
    families: &[String],
) -> Result<Value, String> {
    let mut cmd = spawner_command(repo_root);
    if let Some(st) = state_dir {
        cmd.arg("--state").arg(st);
    }
    cmd.args(["route", "--card", &format!("@{}", card_path.display())]);
    cmd.args(["--role", "review"]);
    for fam in families {
        cmd.args(["--exclude-family", fam]);
    }
    cmd.arg("--json");
    let out = cmd
        .output()
        .map_err(|e| format!("failed to run spawner route: {e}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_owned();
        let stdout = String::from_utf8_lossy(&out.stdout).trim().to_owned();
        return Err(format!(
            "spawner route failed ({}): {}",
            out.status.code().unwrap_or(1),
            if err.is_empty() { stdout } else { err }
        ));
    }
    serde_json::from_slice::<Value>(&out.stdout)
        .map_err(|e| format!("invalid JSON from spawner route: {e}"))
}

fn extract_records(raw: &Value) -> Result<Vec<Value>, String> {
    if let Some(arr) = raw.as_array() {
        return Ok(arr.clone());
    }
    if let Some(arr) = raw.get("runs").and_then(Value::as_array) {
        return Ok(arr.clone());
    }
    if let Some(arr) = raw.get("seats").and_then(Value::as_array) {
        return Ok(arr.clone());
    }
    if raw.is_object() {
        return Ok(vec![raw.clone()]);
    }
    Err("spawner spawn JSON was neither an object nor a list".to_owned())
}

fn load_authoritative_record(rec: Value, state_dir: Option<&Path>) -> (Value, Option<PathBuf>) {
    let run_id = rec["run_id"].as_str().unwrap_or("").to_owned();
    let run_dir = state_dir.and_then(|state| run_dir(state, &run_id).ok());
    let on_disk = run_dir
        .as_ref()
        .map(|d| d.join("record.json"))
        .filter(|p| p.is_file())
        .and_then(|p| fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str::<Value>(&s).ok());
    (on_disk.unwrap_or(rec), run_dir)
}

#[allow(clippy::too_many_arguments)]
fn spawn_one_seat(
    repo_root: &Path,
    state_dir: Option<&Path>,
    work_dir: &Path,
    slot: usize,
    total: usize,
    expected_head: &str,
    card: &Value,
    prompt: &str,
    families: &[String],
    paths: &[String],
    args: &ReviewArgs,
) -> Result<Vec<SeatOutcome>, String> {
    let card_path = work_dir.join(format!("card-{slot}.json"));
    let prompt_path = work_dir.join(format!("prompt-{slot}.txt"));
    fs::write(
        &card_path,
        serde_json::to_string_pretty(card).map_err(|e| format!("serialize card: {e}"))?,
    )
    .map_err(|e| format!("write {}: {e}", card_path.display()))?;
    fs::write(&prompt_path, prompt).map_err(|e| format!("write {}: {e}", prompt_path.display()))?;

    let mut cmd = spawner_command(repo_root);
    if let Some(st) = state_dir {
        cmd.arg("--state").arg(st);
    }
    cmd.current_dir(repo_root);
    cmd.args(["spawn", "--card", &format!("@{}", card_path.display())]);
    cmd.args(["--role", "review"]);
    for fam in families {
        cmd.args(["--exclude-family", fam]);
    }
    cmd.args(["--prompt-file", &prompt_path.display().to_string()]);
    cmd.args(["--cwd", &repo_root.display().to_string()]);
    cmd.args(["--wait", &args.wait_s.to_string()]);
    if args.dry_run {
        cmd.arg("--dry-run");
    }
    if args.seats && total == 1 {
        cmd.arg("--seats");
    }
    cmd.arg("--json");

    let out = cmd
        .output()
        .map_err(|e| format!("failed to run spawner spawn: {e}"))?;
    let raw: Value = serde_json::from_slice(&out.stdout).map_err(|e| {
        let stderr = String::from_utf8_lossy(&out.stderr).trim().to_owned();
        let stdout = String::from_utf8_lossy(&out.stdout).trim().to_owned();
        format!("spawner spawn returned non-JSON ({e}): stderr={stderr} stdout={stdout}")
    })?;
    let records = extract_records(&raw)?;
    records
        .into_iter()
        .map(|rec| {
            let (record, run_dir) = load_authoritative_record(rec, state_dir);
            let seat = evaluate_seat_record(
                slot,
                total,
                expected_head,
                paths,
                &record,
                run_dir.as_deref(),
            );
            if !args.dry_run && seat.verdict == Some(SeatVerdict::Pending) {
                let state = state_dir.ok_or("review state directory unavailable")?;
                let (path, expected_end) =
                    completion::defer(state, expected_head, &seat.run_id, &record)?;
                println!(
                    "PENDING run={} expected_end={} review_file={}",
                    seat.run_id,
                    expected_end,
                    path.display()
                );
            }
            Ok(seat)
        })
        .collect()
}

pub fn format_approval(
    base_sha: &str,
    head_sha: &str,
    item: Option<&str>,
    families: &[String],
    outcomes: &[SeatOutcome],
) -> String {
    let mut lines = vec![
        format!("ITEM: {}", item.unwrap_or("none")),
        format!("BASE: {base_sha}"),
        format!("WRITER_FAMILIES: {}", families.join(",")),
    ];
    for s in outcomes {
        let v_str = s.verdict.map(SeatVerdict::as_str).unwrap_or("MISSING");
        lines.push(format!(
            "SEAT {}/{} run={} status={} exit={} leg={} family={} verdict={} test_gaming={} harness={} files_read={:?} paths={}",
            s.index,
            s.total,
            s.run_id,
            s.status,
            s.exit_code,
            s.leg,
            s.family,
            v_str,
            s.test_gaming.as_str(),
            s.harness,
            s.files_read.as_deref().unwrap_or("none"),
            s.paths_summary
        ));
    }
    lines.push(format!("HEAD: {head_sha}"));
    lines.push("VERDICT: PASS".to_owned());
    lines.push(String::new());
    lines.join("\n")
}

/// One review pass: route and spawn the seats for `families`, split by path when the leg needs it.
/// `group` names the commits this pass covers when the range has several writer families.
fn review_group(
    args: &ReviewArgs,
    resolved: &ResolvedRange,
    state_dir: Option<&Path>,
    work: &Path,
    focus_text: Option<&str>,
    families: &[String],
    group: Option<&WriterGroup>,
) -> Result<Vec<SeatOutcome>, String> {
    let base_card = make_card(
        &resolved.repo_root,
        &resolved.base_sha,
        &resolved.head_sha,
        args.item.as_deref(),
        None,
    );
    let route_card_path = work.join("route-card.json");
    fs::write(
        &route_card_path,
        serde_json::to_string_pretty(&base_card).map_err(|e| format!("serialize card: {e}"))?,
    )
    .map_err(|e| format!("write route card: {e}"))?;

    let route_json = run_route(&resolved.repo_root, state_dir, &route_card_path, families)?;
    let first_leg = route_json
        .get("first_leg")
        .filter(|v| !v.is_null())
        .or_else(|| {
            route_json
                .get("legs")
                .and_then(|l| l.get(0))
                .filter(|v| !v.is_null())
        })
        .cloned()
        .ok_or_else(|| "refused: spawner route returned no eligible review legs".to_owned())?;

    let base_prompt = build_base_prompt(
        &resolved.base_sha,
        &resolved.head_sha,
        args.item.as_deref(),
        focus_text,
        group,
    );
    let measured = measure_diff_files(&resolved.repo_root, &resolved.base_sha, &resolved.head_sha)?;
    let tool_mode = first_leg["tool_mode"].as_str();
    let harness = first_leg["harness"].as_str();
    let mandatory_inline = tool_mode == Some("inline-only") || harness == Some("opencode");
    let check_split = mandatory_inline || (tool_mode.is_none() && harness.is_none());

    let groups: Vec<Vec<String>> = if check_split {
        plan_groups_from_measured(measured, &first_leg, &base_prompt, mandatory_inline)
            .map_err(|reason| format!("refused: {reason}"))?
    } else {
        vec![Vec::new()]
    };

    let total = groups.len();
    let mut outcomes: Vec<SeatOutcome> = std::thread::scope(|scope| {
        let mut handles = Vec::with_capacity(total);
        for (idx, group_paths) in groups.iter().enumerate() {
            let slot = idx + 1;
            let (card, prompt) = if total > 1 {
                (
                    make_card(
                        &resolved.repo_root,
                        &resolved.base_sha,
                        &resolved.head_sha,
                        args.item.as_deref(),
                        Some(group_paths),
                    ),
                    format!("{}{}", group_banner(slot, total, group_paths), base_prompt),
                )
            } else {
                (base_card.clone(), base_prompt.clone())
            };
            let repo_root = &resolved.repo_root;
            let state_ref = state_dir;
            let work_dir = work;
            let fams = families;
            let expected_head = resolved.head_sha.as_str();
            handles.push(scope.spawn(move || {
                spawn_one_seat(
                    repo_root,
                    state_ref,
                    work_dir,
                    slot,
                    total,
                    expected_head,
                    &card,
                    &prompt,
                    fams,
                    group_paths,
                    args,
                )
            }));
        }
        let mut collected = Vec::with_capacity(total);
        for h in handles {
            match h.join() {
                Ok(res) => collected.push(res?),
                Err(_) => return Err("seat worker thread panicked".to_owned()),
            }
        }
        Ok::<Vec<Vec<SeatOutcome>>, String>(collected)
    })?
    .into_iter()
    .flatten()
    .collect();
    outcomes.sort_by_key(|o| o.index);
    Ok(outcomes)
}

pub fn execute(args: &ReviewArgs) -> Result<i32, String> {
    let resolved = resolve_and_check_fresh(&args.repo, &args.base, &args.head, !args.no_fetch)?;
    let groups = writer_groups(&resolved.repo_root, &resolved.base_sha, &resolved.head_sha)?;
    let families: Vec<String> = groups
        .iter()
        .flat_map(|g| g.families.iter().cloned())
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect();
    let focus_text = if let Some(path) = &args.focus {
        Some(
            fs::read_to_string(path)
                .map_err(|e| format!("failed to read --focus {}: {e}", path.display()))?,
        )
    } else {
        None
    };

    let tmp = tempfile::tempdir().map_err(|e| format!("tempdir failed: {e}"))?;
    let state_dir = resolve_state_dir(args.state.as_deref())
        .map(std::path::absolute)
        .transpose()
        .map_err(|error| format!("review state directory: {error}"))?;
    let mut outcomes: Vec<SeatOutcome> = Vec::new();
    if groups.len() == 1 {
        outcomes = review_group(
            args,
            &resolved,
            state_dir.as_deref(),
            tmp.path(),
            focus_text.as_deref(),
            &families,
            None,
        )?;
    } else {
        // Several writer families: one pass per group, its seat from outside that group's families.
        for (n, group) in groups.iter().enumerate() {
            let dir = tmp.path().join(format!("group-{}", n + 1));
            fs::create_dir_all(&dir).map_err(|e| format!("create {}: {e}", dir.display()))?;
            outcomes.extend(review_group(
                args,
                &resolved,
                state_dir.as_deref(),
                &dir,
                focus_text.as_deref(),
                &group.families,
                Some(group),
            )?);
        }
        let total = outcomes.len();
        for (n, seat) in outcomes.iter_mut().enumerate() {
            seat.index = n + 1;
            seat.total = total;
        }
    }

    for s in &outcomes {
        let v_str = s.verdict.map(SeatVerdict::as_str).unwrap_or("MISSING");
        println!(
            "SEAT {}/{} run={} status={} leg={} family={} verdict={} test_gaming={} harness={} files_read={:?} paths={}",
            s.index,
            s.total,
            s.run_id,
            s.status,
            s.leg,
            s.family,
            v_str,
            s.test_gaming.as_str(),
            s.harness,
            s.files_read.as_deref().unwrap_or("none"),
            s.paths_summary
        );
    }

    if args.dry_run {
        println!(
            "REVIEW head={} seats={} verdict=DRY_RUN approval=none",
            resolved.head_sha,
            outcomes.len()
        );
        return Ok(0);
    }

    let has_pending = outcomes
        .iter()
        .any(|seat| seat.verdict == Some(SeatVerdict::Pending));
    let has_failure = outcomes
        .iter()
        .any(|seat| !seat.passed && seat.verdict != Some(SeatVerdict::Pending));
    if has_pending && !has_failure {
        println!(
            "REVIEW head={} seats={} verdict=PENDING approval=none",
            resolved.head_sha,
            outcomes.len()
        );
        return Ok(3);
    }

    let all_pass = !outcomes.is_empty() && outcomes.iter().all(|s| s.passed);
    let approval_path = args.approval.clone().unwrap_or_else(|| {
        resolved
            .git_common_dir
            .join("reviews")
            .join(format!("approval-{}.txt", resolved.head_sha))
    });

    if all_pass {
        let body = format_approval(
            &resolved.base_sha,
            &resolved.head_sha,
            args.item.as_deref(),
            &families,
            &outcomes,
        );
        if let Some(parent) = approval_path.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent)
                .map_err(|e| format!("failed to create approval dir {}: {e}", parent.display()))?;
        }
        atomic::write(&approval_path, body.as_bytes())
            .map_err(|e| format!("failed to write approval {}: {e}", approval_path.display()))?;
        println!(
            "REVIEW head={} seats={} verdict=PASS approval={}",
            resolved.head_sha,
            outcomes.len(),
            approval_path.display()
        );
        Ok(0)
    } else {
        println!(
            "REVIEW head={} seats={} verdict=REQUEST_CHANGES approval=none",
            resolved.head_sha,
            outcomes.len()
        );
        Ok(1)
    }
}

pub fn run(args: &[String]) -> i32 {
    if let [flag, rest @ ..] = args {
        if flag == "--finish-seat" {
            return match completion::finish(rest) {
                Ok(()) => 0,
                Err(error) => {
                    eprintln!("plangraph review: {error}");
                    2
                }
            };
        }
    }
    match parse_args(args) {
        Ok(None) => {
            println!("{}", usage());
            0
        }
        Ok(Some(parsed)) => match timed_review(&parsed) {
            Ok(code) => code,
            Err(err) => {
                eprintln!("plangraph review: {err}");
                2
            }
        },
        Err(err) => {
            eprintln!("plangraph review: {err}");
            2
        }
    }
}

fn timed_review(parsed: &ReviewArgs) -> Result<i32, String> {
    let started = chrono::Utc::now().timestamp_millis();
    let result = execute(parsed);
    if let (Ok(repo), Some(batch), true) = (
        std::env::var("PG_LANDING_REPO"),
        parsed.item.as_deref(),
        parsed.approval.is_some() && !parsed.dry_run,
    ) {
        let end = chrono::Utc::now().timestamp_millis().max(started);
        let body = json!({"action":"stage", "repo":repo, "batch_key":batch,
            "head":parsed.head, "stage":"delta_review", "attempt":started,
            "started_at_ms":started, "ended_at_ms":end});
        if crate::landing::deliver_from_env(body).is_err() {
            eprintln!("delta review timing pending keeper delivery");
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn group_prompt_names_its_commits_and_a_plain_prompt_does_not() {
        let group = WriterGroup {
            families: vec!["google".into()],
            commits: vec!["c1".into(), "c2".into()],
        };
        let named = build_base_prompt("base", "head", None, None, Some(&group));
        assert!(
            named.contains("commits written by google: c1 c2"),
            "{named}"
        );
        let plain = build_base_prompt("base", "head", None, None, None);
        assert!(!plain.contains("commits written by"), "{plain}");
    }
}
