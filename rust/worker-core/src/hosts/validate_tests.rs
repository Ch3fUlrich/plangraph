use super::*;
use crate::slots::{decide, AgingPolicy, Decision, SlotClass, SlotTable, Ticket};
use serde_json::{json, Value};

fn fixture() -> Value {
    json!({"version": 1, "self": "prox", "hosts": [{
        "id": "prox", "class": "restricted", "aliases": [],
        "transport": "keeper", "os": "linux", "state_dir": "/fixture/state",
        "caps": {"max_runs": 4, "max_writers": 2, "max_seats": 2,
                 "max_sessions": 5, "mem_floor_mb": 6144},
        "warmup": {"warmup_s": 1, "ramp_mb": {}},
        "session_mb": 1, "roles": [], "repos": {}
    }]})
}

fn parse(doc: &Value) -> Result<HostRegistry, HostsError> {
    HostRegistry::from_json_str(&doc.to_string())
}

#[test]
fn repo_health_checks_validate_and_roundtrip() {
    let mut doc = fixture();
    let checks = json!({"doctor": ["python3", "doctor"],
        "whoami": ["pg", "whoami"], "spawn": ["pg", "spawn", "--dry-run"]});
    doc["hosts"][0]["repos"]["plangraph"] = json!({
        "root": "/srv/repo", "live_dir": "/srv/live/current",
        "worktrees_root": "/srv/trees", "remote": "example",
        "import_roots": [], "cache_env": {}, "health_checks": checks
    });
    let registry = parse(&doc).unwrap();
    let repo = &registry.self_host().repos["plangraph"];
    assert_eq!(serde_json::to_value(repo).unwrap()["health_checks"], checks);
    doc["hosts"][0]["repos"]["plangraph"]["health_checks"]["doctor"] = json!("bad");
    assert!(matches!(parse(&doc), Err(HostsError::Parse(_))));
}

#[test]
fn class_required_and_schema_agree() {
    let schema: Value =
        serde_json::from_str(include_str!("../../../../docs/schemas/hosts.schema.json")).unwrap();
    let host = &schema["$defs"]["hostEntry"];
    assert!(!host["required"]
        .as_array()
        .unwrap()
        .contains(&json!("class")));
    assert_eq!(
        host["properties"]["class"]["enum"],
        json!(["worker", "restricted"])
    );
    assert_eq!(host["properties"]["class"]["default"], "worker");
    for (label, class) in [
        ("worker", HostClass::Worker),
        ("restricted", HostClass::Restricted),
    ] {
        let mut doc = fixture();
        doc["hosts"][0]["class"] = json!(label);
        assert_eq!(parse(&doc).unwrap().self_host().class, class);
    }
    let mut doc = fixture();
    doc["hosts"][0].as_object_mut().unwrap().remove("class");
    assert_eq!(parse(&doc).unwrap().self_host().class, HostClass::Worker);
}

#[test]
fn unknown_class_refused() {
    let mut doc = fixture();
    doc["hosts"][0]["class"] = json!("unknown");
    assert!(matches!(parse(&doc), Err(HostsError::Parse(_))));
}

#[test]
fn restricted_caps_cannot_widen() {
    for (field, value) in [
        ("max_runs", 5),
        ("max_writers", 3),
        ("max_seats", 3),
        ("max_sessions", 6),
        ("mem_floor_mb", 6143),
    ] {
        let mut doc = fixture();
        doc["hosts"][0]["caps"][field] = json!(value);
        assert!(
            matches!(parse(&doc), Err(HostsError::NumbersShape(msg))
            if msg.contains("field=class/caps")),
            "{field}"
        );
    }
    let mut doc = fixture();
    doc["hosts"][0]["id"] = json!("generic");
    doc["self"] = json!("generic");
    for field in ["max_writers", "max_seats", "max_sessions"] {
        doc["hosts"][0]["caps"][field] = json!(0);
    }
    assert!(parse(&doc).is_ok());
    doc["hosts"][0]["caps"]["max_runs"] = json!(5);
    assert!(matches!(parse(&doc), Err(HostsError::NumbersShape(_))));
}

fn request(class: SlotClass) -> Ticket {
    Ticket {
        run_id: "request".into(),
        pid: 1,
        start_time: 1,
        enqueued_ms: 100,
        role: Role::Implement,
        class,
        repo_id: "fixture".into(),
        reservation_mb: 1,
    }
}

#[test]
fn prox_full_caps_never_grant() {
    let caps = parse(&fixture()).unwrap().self_host().caps.clone();
    for (class, count) in [
        (SlotClass::Other, caps.max_runs),
        (SlotClass::Writer, caps.max_writers),
        (SlotClass::Seat, caps.max_seats),
        (SlotClass::Session, caps.max_sessions),
    ] {
        let mut table = SlotTable::default();
        let ticket = request(class);
        for index in 0..count {
            let Decision::Grant(mut slot) = decide(
                &table,
                &ticket,
                &caps,
                65536,
                |_| 1,
                100,
                &AgingPolicy::default(),
            ) else {
                panic!("below cap")
            };
            slot.run_id = format!("active-{index}");
            table.active.push(slot);
        }
        assert!(matches!(
            decide(
                &table,
                &ticket,
                &caps,
                65536,
                |_| 1,
                100,
                &AgingPolicy::default()
            ),
            Decision::Queue(_)
        ));
    }
}

#[test]
fn prox_floor_never_grants() {
    let caps = parse(&fixture()).unwrap().self_host().caps.clone();
    for class in [
        SlotClass::Other,
        SlotClass::Writer,
        SlotClass::Seat,
        SlotClass::Session,
    ] {
        let ticket = request(class);
        for memory in [caps.mem_floor_mb - 1, caps.mem_floor_mb] {
            assert!(matches!(
                decide(
                    &SlotTable::default(),
                    &ticket,
                    &caps,
                    memory,
                    |_| 0,
                    100,
                    &AgingPolicy::default()
                ),
                Decision::Queue(_)
            ));
        }
        assert!(matches!(
            decide(
                &SlotTable::default(),
                &ticket,
                &caps,
                caps.mem_floor_mb + 1,
                |_| 0,
                100,
                &AgingPolicy::default()
            ),
            Decision::Grant(_)
        ));
    }
}

#[test]
fn worker_caps_unchanged() {
    let mut doc = fixture();
    doc["hosts"][0]["class"] = json!("worker");
    doc["hosts"][0]["caps"] = json!({"max_runs": 8, "max_writers": 6,
        "max_seats": 7, "max_sessions": 9, "mem_floor_mb": 0});
    assert!(parse(&doc).is_ok());
    doc["hosts"][0]["caps"]["max_runs"] = json!(0);
    assert!(matches!(parse(&doc), Err(HostsError::NumbersShape(_))));
}
