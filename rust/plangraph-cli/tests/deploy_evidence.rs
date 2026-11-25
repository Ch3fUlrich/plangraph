//! The deployment evidence report and the GO gate input, against fixture rows and a fake host.
use plangraph_cli::deploy_evidence::{
    build_report, gate, HostProbe, Probed, MAX_AGE_HOURS, SCAN_UNVERIFIED,
};
use serde_json::{json, Value};

/// 2026-11-24T12:00:00Z
const NOW: i64 = 1_795_521_600;

/// The commit every landing of the fixtures reached; the fake host's builds are named by it.
const LANDED: &str = "1111111111111111111111111111111111111111";
const OLD_BUILD: &str = "2222222222222222222222222222222222222222";

struct Host;

impl HostProbe for Host {
    fn state(&self, host: &str, target: &str) -> Result<Probed, String> {
        let probed = |state: &str, revision: Option<&str>| {
            Ok(Probed {
                state: state.into(),
                revision: revision.map(str::to_string),
            })
        };
        match (host, target) {
            ("host-a", "new.service") => probed("inactive", None),
            ("host-a", "bin:tool") => probed("missing", None),
            ("host-a", "running.service") => probed("active", Some(LANDED)),
            ("host-a", "current.service") => probed("active", Some(LANDED)),
            ("host-a", "stale.service") => probed("active", Some(OLD_BUILD)),
            ("host-a", "nameless.service") => probed("active", None),
            _ => Err("ssh refused".into()),
        }
    }

    fn reached(&self, landed: &[String], revision: &str) -> bool {
        revision == LANDED && !landed.is_empty()
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
    assert!(bad(&|r| r["rows"][0]["merged_at"] = json!(7)));
    assert!(bad(&|r| r["generated_at"] = json!(NOW - 7200)));
    assert!(bad(&|r| r["generated_at"] = json!("now")));
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

#[test]
fn a_saved_report_ages_with_the_clock_and_stops_being_accepted() {
    let rows = [
        json!({"key": "demo:G", "state": "done", "merged_at": "2026-11-23T13:00:00Z",
        "age_hours": 1, "missing": ["host-a:new.service"]}),
    ];
    let report = build_report(&rows, &Host, NOW);
    // 23 hours since the merge when written: waiting. An hour later the gate counts 24 and blocks,
    // whatever age the report printed.
    let early = gate(&report, &json!([]), NOW, MAX_AGE_HOURS).unwrap();
    assert_eq!(keys(&early["waiting"]), ["demo:G"]);
    let later = gate(&report, &json!([]), NOW + 3600, MAX_AGE_HOURS).unwrap();
    assert_eq!(keys(&later["blockers"]), ["demo:G"]);
    // Past an hour the report itself is refused.
    assert!(gate(&report, &json!([]), NOW + 3601, MAX_AGE_HOURS).is_err());
}

#[test]
fn a_merge_time_ahead_of_the_clock_is_unknown_not_new() {
    let future = json!([{"key": "demo:H", "state": "done", "merged_at": "2026-11-30T00:00:00Z",
        "age_hours": 0, "missing": ["host-a:new.service"]}]);
    let report = build_report(future.as_array().unwrap(), &Host, NOW);
    let decision = gate(&report, &json!([]), NOW, MAX_AGE_HOURS).unwrap();
    assert_eq!(decision["go"], false);
    assert_eq!(decision["blockers"][0]["age_hours"], Value::Null);
    // Within the clock skew it is a merge that just happened.
    let near = json!([{"key": "demo:H", "state": "done", "merged_at": "2026-11-24T12:02:00Z",
        "age_hours": 0, "missing": ["host-a:new.service"]}]);
    let report = build_report(near.as_array().unwrap(), &Host, NOW);
    assert_eq!(
        gate(&report, &json!([]), NOW, MAX_AGE_HOURS).unwrap()["go"],
        true
    );
}

#[test]
fn the_command_refuses_a_report_at_the_edge_of_the_clock_range_with_exit_2() {
    let dir = tempfile::tempdir().unwrap();
    let report = build_report(&rows(), &Host, NOW);
    for (name, made, now) in [
        ("max", json!(i64::MAX), NOW),
        ("min", json!(i64::MIN), NOW),
        ("late", json!(NOW), i64::MAX),
        ("early", json!(NOW), i64::MIN),
        ("ahead_overflow", json!(i64::MAX), -1),
        ("behind_overflow", json!(i64::MIN), 1),
    ] {
        let mut edited = report.clone();
        edited["generated_at"] = made;
        let path = dir.path().join(format!("{name}.json"));
        std::fs::write(&path, edited.to_string()).unwrap();
        let output = std::process::Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .env_clear()
            .env("HOME", dir.path())
            .args(["deploy-evidence", "gate", "--report"])
            .arg(&path)
            .args(["--now", &now.to_string()])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2), "{name}: {output:?}");
    }
}

fn receipted(unit: &str) -> Value {
    json!({"key": "demo:R", "state": "done", "merged_at": "2026-11-22T07:00:00Z", "age_hours": 53,
           "missing": [], "expected": [format!("host-a:{unit}")], "landed": [LANDED]})
}

#[test]
fn a_receipted_target_is_cleared_only_by_a_host_that_runs_a_build_with_the_landing() {
    let verified = build_report(&[receipted("current.service")], &Host, NOW);
    assert_eq!(verified["rows"][0]["status"], "verified");
    assert_eq!(verified["pending"], 0);
    assert_eq!(
        gate(&verified, &json!([]), NOW, MAX_AGE_HOURS).unwrap()["go"],
        true
    );
}

#[test]
fn a_receipted_target_that_vanished_or_runs_another_build_blocks_go() {
    for (unit, status) in [
        ("new.service", "unit_inactive"), // the unit stopped after the router recorded it
        ("stale.service", "revision_unproven"), // active, but a build without the landing
        ("nameless.service", "revision_unproven"), // active, and it cannot say which build
        ("gone.service", "host_unverified"),
    ] {
        let report = build_report(&[receipted(unit)], &Host, NOW);
        assert_eq!(report["rows"][0]["targets"][0]["status"], status, "{unit}");
        let decision = gate(&report, &json!([]), NOW, MAX_AGE_HOURS).unwrap();
        assert_eq!(decision["go"], false, "{unit}");
        assert_eq!(keys(&decision["blockers"]), ["demo:R"], "{unit}");
    }
}

#[test]
fn a_report_that_hides_a_blocking_target_in_a_verified_row_is_refused() {
    let mut report = build_report(&[receipted("current.service")], &Host, NOW);
    report["rows"][0]["targets"][0]["blocking"] = json!(true);
    assert!(gate(&report, &json!([]), NOW, MAX_AGE_HOURS).is_err());
    let mut report = build_report(&[receipted("new.service")], &Host, NOW);
    report["rows"][0]["status"] = json!("verified");
    report["pending"] = json!(0);
    assert!(gate(&report, &json!([]), NOW, MAX_AGE_HOURS).is_err());
}

#[test]
fn the_command_probe_asks_git_whether_a_build_contains_every_landing() {
    use plangraph_cli::deploy_evidence::CommandProbe;
    let dir = tempfile::tempdir().unwrap();
    let git = |args: &[&str]| {
        let out = std::process::Command::new("git")
            .args(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
            .args(args)
            .current_dir(dir.path())
            .output()
            .unwrap();
        assert!(out.status.success(), "{args:?}: {out:?}");
        String::from_utf8(out.stdout).unwrap().trim().to_string()
    };
    git(&["init", "-q", "-b", "main"]);
    git(&["commit", "-q", "--allow-empty", "-m", "one"]);
    let one = git(&["rev-parse", "HEAD"]);
    git(&["commit", "-q", "--allow-empty", "-m", "two"]);
    let two = git(&["rev-parse", "HEAD"]);
    let probe = CommandProbe {
        program: "unused".into(),
        repo: Some(dir.path().to_str().unwrap().into()),
    };
    assert!(probe.reached(std::slice::from_ref(&one), &two));
    assert!(
        !probe.reached(std::slice::from_ref(&two), &one),
        "an older build lacks the later landing"
    );
    assert!(!probe.reached(&[], &two), "no landing proves nothing");
    assert!(!probe.reached(&["nonsense".into()], &two));
    assert!(!probe.reached(std::slice::from_ref(&one), "not-a-commit"));
    let blind = CommandProbe {
        program: "unused".into(),
        repo: None,
    };
    assert!(
        !blind.reached(std::slice::from_ref(&two), &two),
        "without a checkout no revision is believed"
    );
}
