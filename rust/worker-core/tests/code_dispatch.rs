#[path = "../src/code_dispatch.rs"]
mod code_dispatch;

use code_dispatch::*;
use serde_json::{json, Value};

#[derive(Default)]
struct Fake {
    calls: Vec<String>,
    refuse: Option<String>,
}
impl Backend for Fake {
    fn call(&mut self, verb: &str, _: Value) -> Result<Value, String> {
        self.calls.push(verb.into());
        if self.refuse.as_deref() == Some(verb) {
            return Err("refused".into());
        }
        Ok(match verb {
            "claim" => json!({"lease": "lease-1"}),
            "spawn" => json!({"run_id": "run-1", "harness": "opencode"}),
            _ => json!({}),
        })
    }
}
fn item() -> Item {
    serde_json::from_value(json!({"key":"p:X", "state":"ready", "code_only":true,
        "done":"tests pass", "files":["src/main.rs"], "operator_dependency":false,
        "owner":"owner", "card":{"outcome":"implement"}}))
    .unwrap()
}
fn route() -> Route {
    Route {
        host: "local".into(),
        harness: "opencode".into(),
        cap: 2,
        active: 0,
        usage_percent: 100,
    }
}
#[test]
fn selection_is_explicit_and_fail_closed() {
    let mut i = item();
    assert!(i.eligible());
    i.operator_dependency = true;
    assert!(!i.eligible());
    i.operator_dependency = false;
    i.files.clear();
    assert!(!i.eligible());
    i = item();
    i.done.clear();
    assert!(!i.eligible());
    i = item();
    i.code_only = false;
    assert!(!i.eligible());
    i = item();
    i.owner.clear();
    assert!(!i.eligible());
}
#[test]
fn claim_precedes_spawn_and_handoff_and_cap_is_filled() {
    let mut b = Fake::default();
    let mut second = item();
    second.key = "p:Y".into();
    let mut third = item();
    third.key = "p:Z".into();
    let report = tick(&mut b, vec![item(), second, third], vec![route()]).unwrap();
    assert_eq!(report.started, 2);
    assert_eq!(
        b.calls,
        ["claim", "spawn", "handoff", "claim", "spawn", "handoff"]
    );
}
#[test]
fn refusal_does_not_spawn_or_release_someone_elses_claim() {
    let mut b = Fake {
        refuse: Some("claim".into()),
        ..Fake::default()
    };
    assert_eq!(
        tick(&mut b, vec![item()], vec![route()]).unwrap().started,
        0
    );
    assert_eq!(b.calls, ["claim"]);
}
#[test]
fn spawn_refusal_releases_only_acquired_lease() {
    let mut b = Fake {
        refuse: Some("spawn".into()),
        ..Fake::default()
    };
    tick(&mut b, vec![item()], vec![route()]).unwrap();
    assert_eq!(b.calls, ["claim", "spawn", "release"]);
}
#[test]
fn full_hosts_and_non_opencode_routes_are_refused() {
    let mut b = Fake::default();
    let mut r = route();
    r.active = r.cap;
    tick(&mut b, vec![item()], vec![r]).unwrap();
    let mut r = route();
    r.harness = "claude".into();
    tick(&mut b, vec![item()], vec![r]).unwrap();
    assert!(b.calls.is_empty());
}
#[test]
fn failed_handoff_retains_claim_and_stops_tick() {
    let mut b = Fake {
        refuse: Some("handoff".into()),
        ..Fake::default()
    };
    assert!(tick(&mut b, vec![item(), item()], vec![route()]).is_err());
    assert_eq!(b.calls, ["claim", "spawn", "handoff"]);
}
