//! The deployment evidence report and the GO gate input, against fixture rows and a fake host.
use plangraph_cli::deploy_evidence::{
    build_report, gate, HostProbe, MAX_AGE_HOURS, SCAN_UNVERIFIED,
};
use serde_json::{json, Value};

/// 2026-11-24T12:00:00Z
const NOW: i64 = 1_795_521_600;

struct Host;

impl HostProbe for Host {
    fn state(&self, host: &str, target: &str) -> Result<String, String> {
        match (host, target) {
            ("host-a", "new.service") => Ok("inactive".into()),
            ("host-a", "bin:tool") => Ok("missing".into()),
            ("host-a", "running.service") => Ok("active".into()),
            _ => Err("ssh refused".into()),
        }
    }
}

fn rows() -> Vec<Value> {
    let text = include_str!("fixtures/deploy-evidence-rows.json");
    serde_json::from_str::<Value>(text)
        .unwrap()
        .as_array()
        .unwrap()
        .clone()
}

fn row<'a>(report: &'a Value, key: &str) -> &'a Value {
    report["rows"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["key"] == key)
        .unwrap()
}

fn exception(key: &str, granted: &str, expires: &str) -> Value {
    json!({"key": key, "decision": "D-4001", "granted": granted, "expires": expires,
           "reason": "unit waits for the maintenance window"})
}

fn keys(list: &Value) -> Vec<&str> {
    list.as_array()
        .unwrap()
        .iter()
        .map(|r| r["key"].as_str().unwrap())
        .collect()
}

#[test]
fn an_inactive_unit_and_a_live_clause_without_proof_are_pending() {
    let report = build_report(&rows(), &Host, NOW);
    let inactive = row(&report, "demo:A");
    assert_eq!(inactive["status"], "pending");
    assert_eq!(inactive["targets"][0]["status"], "unit_inactive");
    assert_eq!(inactive["targets"][0]["probe"], "inactive");
    let live = row(&report, "demo:B");
    assert_eq!(live["status"], "pending");
    assert_eq!(live["targets"][0]["status"], "live_proof_missing");
    assert_eq!(
        row(&report, "demo:C")["targets"][0]["status"],
        "binary_missing"
    );
}

#[test]
fn a_unit_that_runs_on_its_host_stays_pending_until_the_keeper_holds_proof_of_the_landed_revision()
{
    let report = build_report(&rows(), &Host, NOW);
    let running = row(&report, "demo:D");
    assert_eq!(running["status"], "pending");
    assert_eq!(running["targets"][0]["status"], "unrecorded");
    assert_eq!(running["targets"][0]["probe"], "active");
    assert_eq!(report["pending"], 5);
    assert!(
        keys(&gate(&report, &json!([]), NOW, MAX_AGE_HOURS).unwrap()["blockers"])
            .contains(&"demo:D")
    );
}

#[test]
fn an_item_the_keeper_could_not_scan_holds_go() {
    let unscanned = json!([{"key": "demo:F", "state": "done", "merged_at": null, "age_hours": null,
        "missing": [format!("{SCAN_UNVERIFIED}: evidence_unverified: deploy_scan_unreadable")]}]);
    let report = build_report(unscanned.as_array().unwrap(), &Host, NOW);
    assert_eq!(
        row(&report, "demo:F")["targets"][0]["status"],
        "scan_unverified"
    );
    assert_eq!(
        gate(&report, &json!([]), NOW, MAX_AGE_HOURS).unwrap()["go"],
        false
    );
}

#[test]
fn a_report_the_gate_cannot_trust_is_refused_not_passed() {
    let honest = build_report(&rows(), &Host, NOW);
    let bad = |edit: &dyn Fn(&mut Value)| {
        let mut report = honest.clone();
        edit(&mut report);
        gate(&report, &json!([]), NOW, MAX_AGE_HOURS).is_err()
    };
    assert!(bad(&|r| {
        r.as_object_mut().unwrap().remove("rows");
    }));
    assert!(bad(&|r| r["rows"] = json!("none")));
    assert!(bad(&|r| r["pending"] = json!(1)));
    assert!(bad(&|r| r["rows"][0]["status"] = json!("ok")));
    assert!(bad(&|r| r["rows"][0]["targets"] = json!([])));
    assert!(bad(
        &|r| r["rows"][0]["targets"][0]["blocking"] = json!(false)
    ));
    assert!(bad(&|r| r["rows"][0]["age_hours"] = json!("old")));
    assert!(bad(&|r| r["rows"][0]["key"] = json!("")));
    assert!(gate(&honest, &json!({}), NOW, MAX_AGE_HOURS).is_err());
    let empty = json!({"schema": "plangraph.deploy-evidence/1", "pending": 1});
    assert!(gate(&empty, &json!([]), NOW, MAX_AGE_HOURS).is_err());
}

#[test]
fn a_host_that_cannot_be_probed_is_not_assumed_deployed() {
    let report = build_report(&rows(), &Host, NOW);
    assert_eq!(
        row(&report, "demo:E")["targets"][0]["status"],
        "host_unverified"
    );
}

#[test]
fn a_pending_deployment_older_than_a_day_blocks_go_and_a_younger_one_waits() {
    let report = build_report(&rows(), &Host, NOW);
    let decision = gate(&report, &json!([]), NOW, MAX_AGE_HOURS).unwrap();
    assert_eq!(decision["go"], false);
    assert_eq!(
        keys(&decision["blockers"]),
        ["demo:A", "demo:B", "demo:D", "demo:E"]
    );
    assert_eq!(keys(&decision["waiting"]), ["demo:C"]);
}

#[test]
fn a_valid_dated_exception_permits_go_only_for_its_own_key() {
    let report = build_report(&rows(), &Host, NOW);
    let all = json!([
        exception("demo:A", "2026-11-23", "2026-11-30"),
        exception("demo:B", "2026-11-23", "2026-11-30"),
        exception("demo:D", "2026-11-23", "2026-11-30"),
        exception("demo:E", "2026-11-24", "2026-11-24"),
    ]);
    let decision = gate(&report, &all, NOW, MAX_AGE_HOURS).unwrap();
    assert_eq!(decision["go"], true);
    assert_eq!(
        keys(&decision["excepted"]),
        ["demo:A", "demo:B", "demo:D", "demo:E"]
    );
    let one = json!([exception("demo:A", "2026-11-23", "2026-11-30")]);
    let decision = gate(&report, &one, NOW, MAX_AGE_HOURS).unwrap();
    assert_eq!(decision["go"], false);
    assert_eq!(keys(&decision["blockers"]), ["demo:B", "demo:D", "demo:E"]);
}

#[test]
fn an_exception_that_is_expired_undated_unnumbered_or_too_long_does_not_count() {
    let report = build_report(&rows()[..1], &Host, NOW);
    let defects = [
        exception("demo:A", "2026-11-01", "2026-11-23"),
        exception("demo:A", "2026-11-25", "2026-11-30"),
        exception("demo:A", "2026-11-01", "2026-12-30"),
        json!({"key": "demo:A", "decision": "D-4001", "reason": "later"}),
        json!({"key": "demo:A", "decision": "yes", "granted": "2026-11-23", "expires": "2026-11-30",
               "reason": "r"}),
        json!({"key": "demo:A", "decision": "D-4001", "granted": "2026-11-23", "expires": "2026-11-30",
               "reason": " "}),
    ];
    for defect in defects {
        let decision = gate(&report, &json!([defect.clone()]), NOW, MAX_AGE_HOURS).unwrap();
        assert_eq!(decision["go"], false, "{defect}");
        assert_eq!(
            keys(&decision["invalid_exceptions"]),
            ["demo:A"],
            "{defect}"
        );
    }
}

#[test]
fn a_report_with_no_pending_row_is_go() {
    let report = build_report(&[], &Host, NOW);
    assert_eq!(
        gate(&report, &json!([]), NOW, MAX_AGE_HOURS).unwrap()["go"],
        true
    );
}
