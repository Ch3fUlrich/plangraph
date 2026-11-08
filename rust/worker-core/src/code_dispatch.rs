//! Fail-closed dispatcher policy. The trusted bridge owns atomic route admission.
use serde::Deserialize;
use serde_json::{json, Value};

pub trait Backend {
    fn call(&mut self, verb: &str, request: Value) -> Result<Value, String>;
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Item {
    pub key: String,
    pub state: String,
    #[serde(default)]
    pub code_only: bool,
    pub done: String,
    pub files: Vec<String>,
    pub operator_dependency: bool,
    pub owner: String,
    pub card: Value,
}
impl Item {
    pub fn eligible(&self) -> bool {
        self.state == "ready"
            && self.code_only
            && !self.operator_dependency
            && !self.key.trim().is_empty()
            && !self.done.trim().is_empty()
            && !self.owner.trim().is_empty()
            && !self.files.is_empty()
            && self.files.iter().all(|p| !p.trim().is_empty())
            && self.card.is_object()
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Route {
    pub host: String,
    pub harness: String,
    pub cap: usize,
    pub active: usize,
    pub usage_percent: u8,
}
#[derive(Default, serde::Serialize)]
pub struct Report {
    pub started: usize,
    pub refused: usize,
}

pub fn tick(
    backend: &mut impl Backend,
    items: Vec<Item>,
    mut routes: Vec<Route>,
) -> Result<Report, String> {
    let mut report = Report::default();
    let mut seen = std::collections::HashSet::new();
    for item in items {
        if !item.eligible() || !seen.insert(item.key.clone()) {
            continue;
        }
        // No Claude route is ever selected; Claude's usage cutoff must not
        // throttle an opencode writer, even at 100 percent reported usage.
        let Some(route) = routes
            .iter_mut()
            .find(|r| r.harness == "opencode" && r.active < r.cap && r.usage_percent <= 100)
        else {
            break;
        };
        let claim = match backend.call(
            "claim",
            json!({
                "key":item.key, "principal":"code-dispatcher", "expected_state":"ready"
            }),
        ) {
            Ok(claim) if claim.get("lease").and_then(Value::as_str).is_some() => claim,
            _ => {
                report.refused += 1;
                continue;
            }
        };
        let request = json!({"key":item.key, "claim":claim,
            "principal":"code-dispatcher", "host":route.host,
            "harness":"opencode", "role":"implement", "card":item.card,
            "capacity_limit":route.cap,
            "completion":{"flow":"GO-REQ", "owner":item.owner},
            "prompt":"Implement only the named files and done criteria. Return the PR to the owner GO-REQ flow; do not merge."});
        let run = match backend.call("spawn", request) {
            Ok(run) => run,
            Err(_) => {
                backend.call(
                    "release",
                    json!({"key":item.key, "claim":claim,
                    "principal":"code-dispatcher", "only_if_no_run":true}),
                )?;
                report.refused += 1;
                continue;
            }
        };
        // An ambiguous success must retain its claim: a writer may be live.
        let run_id = run
            .get("run_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or("spawn returned no run id; claim retained")?;
        if run.get("harness").and_then(Value::as_str) != Some("opencode") {
            return Err("unexpected harness; claim retained".into());
        }
        route.active += 1;
        report.started += 1;
        backend.call(
            "handoff",
            json!({"key":item.key, "claim":claim,
            "run_id":run_id, "owner":item.owner, "flow":"GO-REQ"}),
        )?;
    }
    Ok(report)
}
