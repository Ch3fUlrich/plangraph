use plangraph_cli::feeder::*;
use serde_json::{json, Value};
use std::collections::BTreeSet;
fn facts() -> HostFacts {
    serde_json::from_value(
        json!({"host":"dell","free_ram_bytes":8000000000u64,"free_disk_bytes":20000000000u64,
        "quota_remaining_percent":80,"observed_at":1000,"now":1001}),
    )
    .unwrap()
}
fn ready() -> Value {
    serde_json::from_str(include_str!("fixtures/feeder-ready.json")).unwrap()
}
#[derive(Default)]
struct Fake {
    refused: BTreeSet<String>,
    unavailable: bool,
    fail_start: bool,
    calls: Vec<String>,
}
impl Launch for Fake {
    fn claim(&mut self, k: &str) -> Result<u64, ClaimError> {
        self.calls.push(format!("claim {k}"));
        if self.unavailable {
            Err(ClaimError::Unavailable("keeper offline".into()))
        } else if self.refused.contains(k) {
            Err(ClaimError::Refused("bad_state".into()))
        } else {
            Ok(9)
        }
    }
    fn start(&mut self, c: &Value, _: &str) -> Result<Value, String> {
        self.calls
            .push(format!("start {}", c["item_key"].as_str().unwrap()));
        if self.fail_start {
            Err("launch failed".into())
        } else {
            Ok(json!({"run_id":"run-ok","status":"running"}))
        }
    }
    fn release(&mut self, k: &str, e: u64) -> Result<(), String> {
        self.calls.push(format!("release {k} {e}"));
        Ok(())
    }
}
fn drive(v: &Value, f: &mut Fake, dry: bool) -> Value {
    tick(
        v,
        &[],
        &facts(),
        &Policy::default(),
        "rules",
        "/repo",
        "/reviews",
        dry,
        f,
    )
}
#[test]
fn tick_tries_next_after_item_refusal_but_stops_for_infrastructure_or_start_failure() {
    let mut f = Fake {
        refused: BTreeSet::from(["plangraph:PATCH-PR".into()]),
        ..Fake::default()
    };
    let row = drive(&ready(), &mut f, false);
    assert_eq!(row["item_key"], "plangraph:BUILD-CI-FIX");
    assert_eq!(row["skipped"][0]["item_key"], "plangraph:PATCH-PR");
    assert_eq!(
        f.calls,
        [
            "claim plangraph:PATCH-PR",
            "claim plangraph:BUILD-CI-FIX",
            "start plangraph:BUILD-CI-FIX"
        ]
    );
    let mut f = Fake {
        unavailable: true,
        ..Fake::default()
    };
    assert_eq!(drive(&ready(), &mut f, false)["decision"], "refused");
    assert_eq!(f.calls.len(), 1);
    let mut f = Fake {
        fail_start: true,
        ..Fake::default()
    };
    let row = drive(&ready(), &mut f, false);
    assert_eq!(row["item_key"], "plangraph:PATCH-PR");
    assert!(row["reason"].as_str().unwrap().contains("claim released"));
    assert_eq!(f.calls.last().unwrap(), "release plangraph:PATCH-PR 9");
}
#[test]
fn whole_tick_dry_run_does_not_touch_claim_or_spawner() {
    let mut f = Fake {
        unavailable: true,
        fail_start: true,
        ..Fake::default()
    };
    let row = drive(&ready(), &mut f, true);
    assert_eq!(row["decision"], "would_spawn");
    assert_eq!(row["item_key"], "plangraph:PATCH-PR");
    assert!(f.calls.is_empty());
}

#[test]
fn typed_feeder_brief_preserves_structured_done_criteria() {
    for criteria in [
        json!(["Tests pass", "Document the result"]),
        json!({"tests": "pass", "evidence": "recorded"}),
    ] {
        let mut ready = ready();
        let item = ready["items"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|item| item["key"] == "plangraph:PATCH-PR")
            .unwrap();
        item["done_criteria"] = criteria.clone();

        let mut launch = Fake::default();
        let preview = drive(&ready, &mut launch, true);
        assert_eq!(preview["decision"], "would_spawn", "{preview}");
        let input = pg_worker_core::writer_brief::Input::decode(preview["brief"].as_str().unwrap())
            .unwrap()
            .unwrap();
        assert_eq!(input.items[0].done_criteria, criteria.to_string());
        let started = drive(&ready, &mut launch, false);
        assert_eq!(started["decision"], "spawned", "{started}");
        assert_eq!(started["run"]["run_id"], "run-ok");
        assert_eq!(
            launch.calls,
            ["claim plangraph:PATCH-PR", "start plangraph:PATCH-PR"]
        );
    }
}
#[test]
fn legacy_ready_context_skips_guard_refusals_without_dropping_old_unclaimed_work() {
    let mut v = ready();
    v["items"][4]["context"]["newer_decisions"] = json!(["D-123"]);
    v["items"][3]["context"]["work_exists"] = json!({"commits":[{"sha":"abcd"}],"lanes":[]});
    v["items"][2]["context"]["stale"] = json!("untouched 30d");
    assert_eq!(
        pick(&v, &[], &Policy::default()).unwrap().key,
        "plangraph:HIGH-OLD"
    );
    v["items"][2]["note"] = json!("in_merge_batch: true");
    assert_eq!(
        pick(&v, &[], &Policy::default()).unwrap().key,
        "plangraph:HIGH-NEW"
    );
}
#[test]
fn real_keeper_note_fold_preserves_scope_and_pr_and_ci_metadata() {
    let v = ready();
    let item = decode_task(&v["items"][4]).unwrap();
    assert_eq!(item.files, ["rust/plangraph-cli/src/feeder/"]);
    assert_eq!(item.fields["pr_url"], "https://example.invalid/pulls/123");
    let ci = decode_task(&v["items"][3]).unwrap();
    assert_eq!(ci.fields["ci_fix"], true);
    let mut invalid = v.clone();
    invalid["items"][4]["note"] = json!("files: {\"invalid\":true}");
    let row = drive(&invalid, &mut Fake::default(), true);
    assert_eq!(row["decision"], "would_spawn");
    assert_eq!(row["item_key"], "plangraph:BUILD-CI-FIX");
    assert!(row["skipped"][0]["reason"]
        .as_str()
        .unwrap()
        .contains("metadata"));
}
#[test]
fn real_terminal_statuses_do_not_starve_duplicate_keys() {
    let v = ready();
    for status in [
        "completed",
        "failed",
        "cancelled",
        "killed",
        "dry_run",
        "succeeded",
        "timeout",
        "timed_out",
        "stalled",
        "terminated_after_answer",
        "refused",
        "skipped",
    ] {
        let r = Run {
            item_key: "plangraph:PATCH-PR".into(),
            status: status.into(),
            role: "implement".into(),
            ..Run::default()
        };
        assert!(!r.writer(), "{status}");
        assert_eq!(
            pick(&v, &[r], &Policy::default()).unwrap().key,
            "plangraph:PATCH-PR",
            "{status}"
        );
    }
}
#[test]
fn hyphenated_projects_keep_separate_families_and_filesystems() {
    let v = json!({"items":[{"key":"my-project:KEEPER-A","note":"files: [\"src/a.rs\"]"}]});
    let r = Run {
        item_key: "my-other:KEEPER-B".into(),
        files: vec!["src/a.rs".into()],
        status: "running".into(),
        role: "implement".into(),
        ..Run::default()
    };
    assert!(pick(&v, &[r], &Policy::default()).is_some());
}
#[test]
fn empty_or_false_pr_metadata_is_not_promoted() {
    for signal in [json!(0), json!({}), json!([]), json!(false), json!("")] {
        let v =
            json!({"items":[{"key":"p:A","priority":0,"pr":signal},{"key":"p:B","priority":9}]});
        assert_eq!(pick(&v, &[], &Policy::default()).unwrap().key, "p:B");
    }
}

#[test]
fn metadata_latest_note_wins_and_explicit_fields_override_notes() {
    let v = json!({"key":"p:A","note":"files: [\"old.rs\"]\nfiles: [\"new.rs\"]"});
    assert_eq!(decode_task(&v).unwrap().files, ["new.rs"]);
    let mut v = v;
    v["files"] = json!(["explicit.rs"]);
    assert_eq!(decode_task(&v).unwrap().files, ["explicit.rs"]);
    let v = json!({"items":[{"key":"p:A","priority":0,"pull_request":{"url":"https://example.invalid/pulls/1","state":"closed"}},
        {"key":"p:B","priority":9}]});
    assert_eq!(pick(&v, &[], &Policy::default()).unwrap().key, "p:B");
}

#[test]
fn scalar_files_and_bad_candidates_cannot_starve_other_work() {
    let item = decode_task(&json!({"key":"p:A","note":"files: src/a.rs"})).unwrap();
    assert_eq!(item.files, ["src/a.rs"]);
    let v = json!({"items":[{"key":"p:BAD","files":{"invalid":true},"priority":1000},
        {"key":"p:GOOD","priority":1}]});
    let row = drive(&v, &mut Fake::default(), true);
    assert_eq!(row["item_key"], "p:GOOD");
    assert_eq!(row["skipped"][0]["item_key"], "p:BAD");
    let row = drive(
        &json!({"items":[{"key":"p:BAD","files":42}]}),
        &mut Fake::default(),
        true,
    );
    assert_eq!(row["decision"], "idle");
    assert_eq!(row["skipped"].as_array().unwrap().len(), 1);
}

#[test]
fn promotion_signals_reject_negative_words_and_ci_fixtures() {
    for word in ["none", "NONE", "n/a", "n-a", "no", "No", "null", "false"] {
        let v =
            json!({"items":[{"key":"p:A","priority":0,"open_pr":word},{"key":"p:B","priority":9}]});
        assert_eq!(pick(&v, &[], &Policy::default()).unwrap().key, "p:B");
    }
    let v = json!({"items":[{"key":"p:CI-FIXTURES","priority":0},{"key":"p:OTHER","priority":9}]});
    assert_eq!(pick(&v, &[], &Policy::default()).unwrap().key, "p:OTHER");
}

#[test]
fn brief_keeps_pr_context_and_plain_done_criteria() {
    let item = decode_task(
        &json!({"key":"p:A","done_criteria":"first line\nsecond line",
        "note":"pr_url: https://example.invalid/pulls/1\nci_fix: true"}),
    )
    .unwrap();
    let b = brief(&item, "rules");
    assert!(b.contains("Done criteria:\nfirst line\nsecond line"));
    assert!(b.contains("https://example.invalid/pulls/1"));
    assert!(b.contains("ci_fix: true"));
}

#[test]
fn claim_refusal_loop_is_bounded_and_unavailable_claim_is_audited_without_blind_release() {
    let v = ready();
    let keys: BTreeSet<String> = v["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| i["key"].as_str().unwrap().into())
        .collect();
    let mut f = Fake {
        refused: keys.clone(),
        ..Fake::default()
    };
    let row = drive(&v, &mut f, false);
    assert_eq!(row["decision"], "idle");
    assert_eq!(row["skipped"].as_array().unwrap().len(), keys.len());
    assert_eq!(f.calls.len(), keys.len());
    let mut f = Fake {
        unavailable: true,
        ..Fake::default()
    };
    let row = drive(&v, &mut f, false);
    assert_eq!(row["item_key"], "plangraph:PATCH-PR");
    assert_eq!(row["error"], true);
    assert!(row["reason"].as_str().unwrap().contains("unavailable"));
    assert_eq!(f.calls, ["claim plangraph:PATCH-PR"]);
}
