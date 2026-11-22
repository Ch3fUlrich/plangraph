use super::{
    paths::{resolve, var},
    process::capture,
    role_of,
};
use regex::Regex;
use serde_json::Value;
use std::{
    path::Path,
    process::Command,
    sync::LazyLock,
    thread,
    time::{Duration, Instant},
};

static UUID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}").unwrap()
});
static SHORT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"backgrounded\W+([0-9a-f]{8})\b").unwrap());

pub(super) fn session_id(stdout: &str, name: &str, launched_ms: u128, cwd: &str) -> Option<String> {
    if let Some(id) = UUID.find(stdout) {
        return Some(id.as_str().into());
    }
    let short = SHORT
        .captures(stdout)
        .and_then(|m| m.get(1))
        .map(|m| m.as_str());
    let budget = var("FLEET_RESOLVE_BUDGET")
        .and_then(|s| s.trim().parse::<f64>().ok())
        .filter(|s| s.is_finite() && *s >= 0.0)
        .unwrap_or(15.0)
        .min(600.0);
    let deadline = Instant::now() + Duration::from_secs_f64(budget);
    let cwd = resolve(Path::new(cwd));
    let wait = Duration::from_millis(500);
    loop {
        let timeout = deadline
            .saturating_duration_since(Instant::now())
            .clamp(Duration::from_millis(100), Duration::from_secs(5));
        let rows = capture(
            Command::new("claude")
                .args(["agents", "--json"])
                .env_remove("FLEET_FORWARDED"),
            None,
            timeout,
        )
        .ok()
        .filter(|out| out.status.success())
        .and_then(|out| serde_json::from_slice::<Vec<Value>>(&out.stdout).ok())
        .unwrap_or_default();
        if let Some(id) = find(&rows, short, name, launched_ms, &cwd) {
            return Some(id.into());
        }
        if Instant::now() + wait >= deadline {
            return None;
        }
        thread::sleep(wait);
    }
}

fn find<'a>(
    rows: &'a [Value],
    short: Option<&str>,
    name: &str,
    launched_ms: u128,
    cwd: &Path,
) -> Option<&'a str> {
    let role = role_of(name);
    let candidates = rows.iter().filter_map(|row| {
        let sid = row["sessionId"].as_str()?;
        let path = resolve(Path::new(row["cwd"].as_str().filter(|p| !p.is_empty())?));
        let worktrees = cwd.join(".claude/worktrees");
        let matches = path == cwd || (path != worktrees && path.starts_with(worktrees));
        (row["kind"] == "background" && !sid.is_empty() && matches).then_some((row, sid))
    });
    let sid = if let Some(short) = short {
        candidates
            .filter(|(row, sid)| row["id"] == short || sid.starts_with(short))
            .map(|(_, sid)| sid)
            .next()
    } else {
        let mut newest = None;
        for (row, sid) in candidates.filter(|(row, _)| row["name"] == name || row["name"] == role) {
            let Some(start) = row["startedAt"]
                .as_f64()
                .filter(|t| *t >= launched_ms as f64 - 5000.0)
            else {
                continue;
            };
            if newest.is_none_or(|(time, _)| start > time) {
                newest = Some((start, sid));
            }
        }
        newest.map(|(_, sid)| sid)
    }?;
    (sid.len() == 36 && UUID.is_match(sid)).then_some(sid)
}
