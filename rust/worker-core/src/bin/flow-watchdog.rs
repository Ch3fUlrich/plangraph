#[path = "flow_watchdog_hosts/mod.rs"]
mod hosts;
#[path = "flow_watchdog_quiet/mod.rs"]
mod quiet;
#[path = "flow_watchdog_stuck/mod.rs"]
mod stuck;
include!("flow_watchdog_stuck/legacy_tests.rs");
// Deterministic flow checks; host collectors supply a single JSON snapshot.
use serde::Deserialize;
use std::{
    fs,
    io::Write,
    path::Path,
    process::{Command, Stdio},
};
#[derive(Deserialize)]
struct Snapshot {
    #[serde(default)]
    hosts: Vec<hosts::Host>,
    #[serde(flatten)]
    stuck: stuck::Snapshot,
    #[serde(flatten)]
    session_activity: quiet::Reader,
    pace: f64,
    budget: f64,
    ci_failed: u64,
    ci_total: u64,
    infra_failed: u64,
    review_age_hours: f64,
    review_rounds: u64,
    critical_idle_hours: f64,
    coding_vm_runs: u64,
    worker_idle: bool,
    host_drift: bool,
    closes_recent: u64,
    closes_previous: u64,
}
struct Alert {
    message: String,
    active: String,
}
impl From<String> for Alert {
    fn from(message: String) -> Self {
        Self {
            active: message.clone(),
            message,
        }
    }
}
fn pace(s: &Snapshot) -> bool {
    s.pace > s.budget
}
fn ci(s: &Snapshot) -> bool {
    let total = s.ci_total.saturating_sub(s.infra_failed);
    total > 0 && s.ci_failed.saturating_sub(s.infra_failed) as f64 / total as f64 > 0.1
}
fn age(s: &Snapshot) -> bool {
    s.review_age_hours > 24.0
}
fn rounds(s: &Snapshot) -> bool {
    s.review_rounds > 3
}
fn critical(s: &Snapshot) -> bool {
    s.critical_idle_hours > 12.0
}
fn placement(s: &Snapshot) -> bool {
    s.coding_vm_runs > 0 && s.worker_idle
}
fn drift(s: &Snapshot) -> bool {
    s.host_drift
}
fn closes(s: &Snapshot) -> bool {
    s.closes_recent < s.closes_previous
}
fn findings(s: &Snapshot) -> Vec<&'static str> {
    type Check = (&'static str, fn(&Snapshot) -> bool);
    let checks: [Check; 8] = [
        ("claude-pace", pace),
        ("ci-fail-rate", ci),
        ("review-age", age),
        ("review-rounds", rounds),
        ("critical-path-idle", critical),
        ("placement", placement),
        ("host-drift", drift),
        ("closes-trend", closes),
    ];
    checks
        .into_iter()
        .filter_map(|(name, check)| check(s).then_some(name))
        .collect()
}
fn atomic(path: &Path, text: &str) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, text)?;
    fs::rename(tmp, path)
}
fn deliver(variable: &str, event: &str) -> Result<bool, Box<dyn std::error::Error>> {
    let Ok(value) = std::env::var(variable) else {
        return Ok(false);
    };
    let argv: Vec<String> = serde_json::from_str(&value)?;
    let (program, args) = argv.split_first().ok_or("empty delivery argv")?;
    let mut child = Command::new(program)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .spawn()?;
    let written = child
        .stdin
        .take()
        .ok_or("missing stdin")?
        .write_all(event.as_bytes());
    if !child.wait()?.success() {
        return Ok(false);
    }
    written?;
    Ok(true)
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 {
        return Err("usage: flow-watchdog SNAPSHOT.json STATE_DIR".into());
    }
    let validated = (|| -> Result<Snapshot, Box<dyn std::error::Error>> {
        let s: Snapshot = serde_json::from_slice(&fs::read(&args[1])?)?;
        if s.infra_failed > s.ci_failed || s.ci_failed > s.ci_total {
            return Err("inconsistent CI counts".into());
        }
        if s.budget <= 0.0 {
            return Err("budget must be positive".into());
        }
        Ok(s)
    })();
    if let Err(e) = &validated {
        eprintln!("FLOW-WATCHDOG: invalid snapshot: {e}");
    }
    let dir = Path::new(&args[2]);
    fs::create_dir_all(dir)?;
    let pending = dir.join("pending.json");
    let messages = match &validated {
        Ok(s) => {
            let mut messages = s.stuck.messages();
            messages.extend(hosts::messages(&s.hosts));
            let legacy = findings(s).join(",");
            if !legacy.is_empty() {
                messages.insert(0, legacy);
            }
            let mut alerts: Vec<Alert> = messages.into_iter().map(Alert::from).collect();
            alerts.extend(s.session_activity.alerts());
            alerts
        }
        Err(_) => vec![Alert::from("invalid snapshot".to_owned())],
    };
    let previous = fs::read_to_string(dir.join("active")).unwrap_or_default();
    let active = messages
        .iter()
        .map(|alert| alert.active.as_str())
        // Validation failure is not an observed recovery for quiet sessions.
        .chain(
            previous
                .lines()
                .filter(|line| validated.is_err() && line.starts_with("busy-quiet ")),
        )
        .collect::<Vec<_>>()
        .join("\n");
    let queue_path = dir.join("queue.json");
    let mut queue: Vec<serde_json::Value> = match fs::read(&queue_path) {
        Ok(bytes) => serde_json::from_slice(&bytes)?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(e) => return Err(e.into()),
    };
    if active != previous {
        for alert in &messages {
            if previous.lines().any(|line| line == alert.active) {
                continue;
            }
            queue.push(serde_json::json!({"kind":"alert", "recipient":"routing",
                "message":format!("FLOW-WATCHDOG: {}", alert.message), "active":alert.active}));
            atomic(&queue_path, &serde_json::to_string(&queue)?)?;
        }
        atomic(&dir.join("active"), &active)?;
    }
    // Routing advances independently of the oldest push retry. Persist each ack.
    for i in 0..queue.len() {
        if queue[i]["routing_done"] != true
            && !(i == 0 && pending.exists() && dir.join("routing.ok").exists())
            && deliver("PLANGRAPH_WATCHDOG_DELIVERY", &queue[i].to_string()).unwrap_or(false)
        {
            queue[i]["routing_done"] = true.into();
            atomic(&queue_path, &serde_json::to_string(&queue)?)?;
        }
    }
    // Keep the pending head and the newest transitions; overflow is explicit.
    if queue.len() > 256 {
        queue.drain(1..queue.len() - 255);
        eprintln!("FLOW-WATCHDOG: backlog capped at 256 events; oldest queued transitions dropped");
        atomic(&queue_path, &serde_json::to_string(&queue)?)?;
    }
    loop {
        if !pending.exists() {
            let Some(event) = queue.first() else { break };
            // Reset checkpoints before publishing an event. A crash may duplicate,
            // but cannot acknowledge a fresh event using stale checkpoints.
            for channel in ["routing.ok", "push.ok"] {
                match fs::remove_file(dir.join(channel)) {
                    Ok(()) => (),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
                    Err(e) => return Err(format!("reset {channel}: {e}").into()),
                }
            }
            atomic(&pending, &event.to_string())?;
            if event["routing_done"] == true {
                atomic(&dir.join("routing.ok"), "ok")?;
            }
        }
        let event = fs::read_to_string(&pending)?;
        for (marker, variable) in [
            ("routing.ok", "PLANGRAPH_WATCHDOG_DELIVERY"),
            ("push.ok", "PLANGRAPH_FLOW_PUSH"),
        ] {
            if !dir.join(marker).exists() && deliver(variable, &event).unwrap_or(false) {
                atomic(&dir.join(marker), "ok")?;
            }
        }
        if !dir.join("routing.ok").exists() || !dir.join("push.ok").exists() {
            break;
        }
        // Remove pending first: interruption can repeat delivery, never lose it.
        fs::remove_file(&pending)?;
        let delivered: serde_json::Value = serde_json::from_str(&event)?;
        if queue.first().is_some_and(|v| *v == delivered) {
            queue.remove(0);
            atomic(&queue_path, &serde_json::to_string(&queue)?)?;
        }
    }
    validated.map(|_| ())
}
