//! Deterministic flow checks; host collectors supply a single JSON snapshot.
use serde::Deserialize;
use std::{fs, io::Write, path::Path, process::{Command, Stdio}};

#[derive(Deserialize)]
struct Snapshot {
    pace: f64, budget: f64,
    ci_failed: u64, ci_total: u64, infra_failed: u64,
    review_age_hours: f64, review_rounds: u64, critical_idle_hours: f64,
    coding_vm_runs: u64, dell_idle: bool, host_drift: bool,
    closes_recent: u64, closes_previous: u64,
}
fn pace(s: &Snapshot) -> bool { s.budget > 0.0 && s.pace > s.budget }
fn ci(s: &Snapshot) -> bool {
    let total = s.ci_total.saturating_sub(s.infra_failed);
    total > 0 && s.ci_failed.saturating_sub(s.infra_failed) as f64 / total as f64 > 0.1
}
fn age(s: &Snapshot) -> bool { s.review_age_hours > 24.0 }
fn rounds(s: &Snapshot) -> bool { s.review_rounds > 3 }
fn critical(s: &Snapshot) -> bool { s.critical_idle_hours > 12.0 }
fn placement(s: &Snapshot) -> bool { s.coding_vm_runs > 0 && s.dell_idle }
fn drift(s: &Snapshot) -> bool { s.host_drift }
fn closes(s: &Snapshot) -> bool { s.closes_recent < s.closes_previous }
fn findings(s: &Snapshot) -> Vec<&'static str> {
    let checks: [(&str, fn(&Snapshot) -> bool); 8] = [
        ("claude-pace", pace), ("ci-fail-rate", ci), ("review-age", age),
        ("review-rounds", rounds), ("critical-path-idle", critical),
        ("placement", placement), ("host-drift", drift), ("closes-trend", closes)];
    checks.into_iter().filter_map(|(name, check)| check(s).then_some(name)).collect()
}
fn atomic(path: &Path, text: &str) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, text)?;
    fs::rename(tmp, path)
}
fn deliver(variable: &str, event: &str) -> Result<bool, Box<dyn std::error::Error>> {
    let Ok(value) = std::env::var(variable) else { return Ok(false) };
    let argv: Vec<String> = serde_json::from_str(&value)?;
    let (program, args) = argv.split_first().ok_or("empty delivery argv")?;
    let mut child = Command::new(program).args(args).stdin(Stdio::piped())
        .stdout(Stdio::null()).spawn()?;
    child.stdin.take().ok_or("missing stdin")?.write_all(event.as_bytes())?;
    Ok(child.wait()?.success())
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 { return Err("usage: flow-watchdog SNAPSHOT.json STATE_DIR".into()) }
    let s: Snapshot = serde_json::from_slice(&fs::read(&args[1])?)?;
    if s.infra_failed > s.ci_failed || s.ci_failed > s.ci_total {
        return Err("inconsistent CI counts".into());
    }
    let dir = Path::new(&args[2]);
    fs::create_dir_all(dir)?;
    let pending = dir.join("pending.json");
    let active = findings(&s).join(",");
    // Finish both channels before accepting another transition. Successful channels
    // are checkpointed independently so push retries do not repeat routing alerts.
    if !pending.exists() {
        let previous = fs::read_to_string(dir.join("active")).unwrap_or_default();
        if active == previous { return Ok(()) }
        if active.is_empty() { atomic(&dir.join("active"), "")?; return Ok(()) }
        let event = serde_json::json!({"kind":"alert", "recipient":"routing",
            "message":format!("FLOW-WATCHDOG: {active}"), "active":active});
        atomic(&pending, &event.to_string())?;
        for channel in ["routing.ok", "push.ok"] {
            match fs::remove_file(dir.join(channel)) {
                Ok(()) => (), Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
                Err(e) => return Err(e.into()),
            }
        }
    }
    let event = fs::read_to_string(&pending)?;
    for (marker, variable) in [("routing.ok", "PLANGRAPH_WATCHDOG_DELIVERY"),
        ("push.ok", "PLANGRAPH_FLOW_PUSH")] {
        if !dir.join(marker).exists() && deliver(variable, &event)? {
            atomic(&dir.join(marker), "ok")?;
        }
    }
    if dir.join("routing.ok").exists() && dir.join("push.ok").exists() {
        let value: serde_json::Value = serde_json::from_str(&event)?;
        atomic(&dir.join("active"), value["active"].as_str().ok_or("bad pending event")?)?;
        fs::remove_file(pending)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn each_rule_has_below_boundary_above() {
        let mut s = Snapshot { pace:65.0,budget:66.0,ci_failed:0,ci_total:10,
            infra_failed:0,review_age_hours:23.0,review_rounds:2,
            critical_idle_hours:11.0,coding_vm_runs:0,dell_idle:true,
            host_drift:false,closes_recent:5,closes_previous:4 };
        assert!(findings(&s).is_empty());
        s.pace=66.0; s.ci_failed=1; s.review_age_hours=24.0;
        s.review_rounds=3; s.critical_idle_hours=12.0; s.closes_recent=4;
        assert!(findings(&s).is_empty());
        s.pace=242.0; s.ci_failed=2; s.review_age_hours=25.0;
        s.review_rounds=4; s.critical_idle_hours=13.0; s.coding_vm_runs=1;
        s.host_drift=true; s.closes_recent=3;
        assert_eq!(findings(&s).len(),8);
        s.infra_failed=2; assert!(!ci(&s));
        s.dell_idle=false; assert!(!placement(&s));
        s.ci_total=0; s.ci_failed=0; s.infra_failed=0; assert!(!ci(&s));
    }
}
