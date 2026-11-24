use plangraph_cli::feeder::*;
use serde_json::{json, Value};

fn ready() -> Value {
    serde_json::from_str(include_str!("fixtures/feeder-ready.json")).unwrap()
}
fn winner(v: &Value, runs: &[Run]) -> Option<String> {
    pick(v, runs, &Policy::default()).map(|t| t.key)
}
fn run(key: &str, files: &[&str]) -> Run {
    Run {
        run_id: "run-fixture".into(),
        started: Some(900.0),
        item_key: key.into(),
        host: "dell".into(),
        status: "running".into(),
        role: "implement".into(),
        files: files.iter().map(|s| (*s).into()).collect(),
        item_keys: vec![],
        ..Run::default()
    }
}

#[test]
fn pick_orders_pr_ci_priority_and_age_without_claim_priority_changes() {
    let mut v = ready();
    for expected in [
        "PATCH-PR",
        "BUILD-CI-FIX",
        "HIGH-OLD",
        "HIGH-NEW",
        "LOW-OLD",
    ] {
        let key = winner(&v, &[]).unwrap();
        assert_eq!(key, format!("plangraph:{expected}"));
        v["items"]
            .as_array_mut()
            .unwrap()
            .retain(|i| i["key"] != key);
    }
    assert_eq!(winner(&v, &[]), None);
    let mut v = ready();
    v["items"][4]["claimed_by"] = json!("previous-feeder");
    v["items"][4]["updated_at"] = json!("2026-11-15T12:00:00Z");
    assert_eq!(winner(&v, &[]).unwrap(), "plangraph:PATCH-PR");
}

#[test]
fn pick_never_selects_reconciliation_in_progress_claims_done_or_batch_items() {
    let mut v = ready();
    v["needs_reconcile"] = json!([{"key":"plangraph:PATCH-PR"}]);
    v["in_progress"] = json!(["plangraph:BUILD-CI-FIX"]);
    v["items"][2]["state"] = json!("claimed");
    v["items"][1]["state"] = json!("done");
    assert_eq!(winner(&v, &[]).unwrap(), "plangraph:LOW-OLD");
    // A merge batch is still an active run, even if not currently executing.
    let mut batch = run("plangraph:LOW-OLD", &[]);
    batch.status = "waiting_merge_batch".into();
    assert_eq!(winner(&v, &[batch]), None);
}

#[test]
fn pick_skips_exact_runs_families_and_file_or_directory_overlap() {
    let v = ready();
    for r in [
        run("plangraph:PATCH-PR", &[]),
        run("plangraph:PATCH-OTHER", &[]),
        run(
            "plangraph:UNRELATED",
            &["rust/plangraph-cli/src/feeder/mod.rs"],
        ),
        run("plangraph:UNRELATED", &["rust/plangraph-cli/src"]),
    ] {
        assert_eq!(winner(&v, &[r]).unwrap(), "plangraph:BUILD-CI-FIX");
    }
    let mut completed = run("plangraph:PATCH-PR", &[]);
    completed.status = "completed".into();
    assert_eq!(winner(&v, &[completed]).unwrap(), "plangraph:PATCH-PR");
    let mut review = run("plangraph:OTHER", &["rust/plangraph-cli/src"]);
    review.role = "review".into();
    assert_eq!(winner(&v, &[review]).unwrap(), "plangraph:PATCH-PR");
    // Even a reviewer cannot start a second run for the identical item.
    assert_eq!(
        winner(
            &v,
            &[Run {
                role: "review".into(),
                ..run("plangraph:PATCH-PR", &[])
            }]
        )
        .unwrap(),
        "plangraph:BUILD-CI-FIX"
    );
    assert_eq!(winner(&v, &[run("", &[])]).unwrap(), "plangraph:PATCH-PR");
}

fn facts() -> HostFacts {
    HostFacts {
        host: "dell".into(),
        free_ram_bytes: 8_000_000_000,
        free_disk_bytes: 20_000_000_000,
        quota_remaining_percent: Some(80.0),
        observed_at: 1000,
        now: 1001,
        running_writers: 2,
        kill_switch: false,
    }
}
#[test]
fn admit_all_refusal_reasons_and_boundaries() {
    let p = Policy::default();
    assert_eq!(admit(&facts(), &p), Admission::Admit);
    let cases = [
        (
            HostFacts {
                kill_switch: true,
                ..facts()
            },
            "kill_switch",
        ),
        (
            HostFacts {
                observed_at: 1,
                ..facts()
            },
            "stale_facts",
        ),
        (
            HostFacts {
                observed_at: 1002,
                ..facts()
            },
            "stale_facts",
        ),
        (
            HostFacts {
                host: "unknown".into(),
                ..facts()
            },
            "unknown_host",
        ),
        (
            HostFacts {
                running_writers: 5,
                ..facts()
            },
            "max_writers",
        ),
        (
            HostFacts {
                host: "coding.example".into(),
                running_writers: 3,
                ..facts()
            },
            "max_writers",
        ),
        (
            HostFacts {
                free_ram_bytes: p.ram_per_run_bytes - 1,
                ..facts()
            },
            "ram",
        ),
        (
            HostFacts {
                free_disk_bytes: p.disk_floor_bytes - 1,
                ..facts()
            },
            "disk",
        ),
        (
            HostFacts {
                quota_remaining_percent: Some(p.quota_floor_percent),
                ..facts()
            },
            "provider_quota",
        ),
        (
            HostFacts {
                quota_remaining_percent: None,
                ..facts()
            },
            "provider_quota",
        ),
        (
            HostFacts {
                quota_remaining_percent: Some(f64::NAN),
                ..facts()
            },
            "provider_quota",
        ),
        (
            HostFacts {
                quota_remaining_percent: Some(101.0),
                ..facts()
            },
            "provider_quota",
        ),
    ];
    for (f, reason) in cases {
        assert_eq!(admit(&f, &p), Admission::Refuse(reason.into()));
    }
    assert_eq!(
        admit(
            &HostFacts {
                free_ram_bytes: p.ram_per_run_bytes,
                free_disk_bytes: p.disk_floor_bytes,
                observed_at: 701,
                ..facts()
            },
            &p
        ),
        Admission::Admit
    );
}

#[test]
fn brief_is_deterministic_and_preserves_every_house_rule_and_item_field() {
    let item = pick(&ready(), &[], &Policy::default()).unwrap();
    let rules = include_str!("../../../deploy/feeder/house-rules.md");
    let text = brief(&item, rules);
    assert_eq!(text, brief(&item, rules));
    for field in [
        &item.key,
        &item.title,
        item.description.as_ref().unwrap(),
        &item.files[0],
        item.done_criteria.as_str().unwrap(),
    ] {
        assert!(text.contains(field), "{field}");
    }
    for rule in [
        "Rust with Rust tests",
        "No new Python",
        "tools/new_code_allowlist.txt",
        "D-id",
        "hooks as installed",
        "never bypass hooks",
        "Never push",
        "D-5969",
        "fast-checks.sh",
        "agy-review.sh",
        "REVIEW_WORKTREE",
        "origin/main",
        "FINDING/VERDICT",
        "TEST_GAMING",
        "no background",
        "same session",
        "at most 8 rounds",
        "max 10 lines",
        "role\nreview",
        "writer_families listing the family of every Writer trailer",
        "Plan-Item: plangraph:PATCH-PR",
        "Writer: codex/gpt-6.2-sol-medium",
        "Changelog:",
        "ONE final paragraph",
    ] {
        assert!(text.contains(rule), "missing rule: {rule}");
    }
    assert!(!text.contains("{{item_key}}"));
    let c = card(&item, "/repo", "/reviews", &Policy::default());
    assert_eq!(
        c,
        json!({"run_kind":"native-codex","repo":"/repo","base_ref":"refs/heads/main",
        "item_key":item.key,"review_dir":"/reviews","estimate_tokens":100000,"files":item.files})
    );
}

#[derive(Default)]
struct Fake {
    calls: Vec<String>,
    fail_claim: bool,
    fail_spawn: bool,
    fail_release: bool,
}
impl Launch for Fake {
    fn claim(&mut self, key: &str) -> Result<u64, ClaimError> {
        self.calls.push(format!("claim {key}"));
        if self.fail_claim {
            Err(ClaimError::Refused("claim denied".into()))
        } else {
            Ok(7)
        }
    }
    fn start(&mut self, card: &Value, brief: &str) -> Result<Value, String> {
        self.calls.push(format!(
            "spawn {} {brief}",
            card["item_key"].as_str().unwrap()
        ));
        if self.fail_spawn {
            Err("spawner denied".into())
        } else {
            Ok(json!({"run_id":"run-1"}))
        }
    }
    fn release(&mut self, key: &str, epoch: u64) -> Result<(), String> {
        self.calls.push(format!("release {key} {epoch}"));
        if self.fail_release {
            Err("release denied".into())
        } else {
            Ok(())
        }
    }
}
#[test]
fn spawn_compensates_failure_with_the_claim_epoch_and_reports_release_failure() {
    let c = json!({"item_key":"plangraph:A"});
    let mut fake = Fake {
        fail_spawn: true,
        ..Fake::default()
    };
    assert!(spawn(&c, "brief", &mut fake)
        .unwrap_err()
        .to_string()
        .contains("claim released"));
    assert_eq!(
        fake.calls,
        [
            "claim plangraph:A",
            "spawn plangraph:A brief",
            "release plangraph:A 7"
        ]
    );
    fake.calls.clear();
    fake.fail_release = true;
    assert!(spawn(&c, "brief", &mut fake)
        .unwrap_err()
        .to_string()
        .contains("release failed"));
    assert_eq!(fake.calls.len(), 3);
}
#[test]
fn spawn_stops_on_claim_refusal_and_keeps_successful_claim() {
    let c = json!({"item_key":"plangraph:A"});
    let mut fake = Fake {
        fail_claim: true,
        ..Fake::default()
    };
    assert!(spawn(&c, "brief", &mut fake).is_err());
    assert_eq!(fake.calls, ["claim plangraph:A"]);
    let mut fake = Fake::default();
    assert_eq!(spawn(&c, "brief", &mut fake).unwrap()["run_id"], "run-1");
    assert_eq!(fake.calls.len(), 2);
}
#[test]
fn dry_run_does_not_claim_spawn_or_release() {
    let mut fake = Fake {
        fail_claim: true,
        fail_spawn: true,
        fail_release: true,
        ..Fake::default()
    };
    let c = json!({"item_key":"plangraph:A"});
    let decision = dispatch(true, &c, "brief", &mut fake).unwrap();
    assert_eq!(decision["decision"], "would_spawn");
    assert_eq!(decision["card"], c);
    assert!(fake.calls.is_empty());
}

#[test]
fn live_nullable_run_fields_decode_and_still_exclude_unknown_scope() {
    let r: Run = serde_json::from_value(json!({"item_key":null,"role":"implement",
        "host":"dell","status":"running","files":null}))
    .unwrap();
    assert!(r.writer());
    assert!(r.item_key.is_empty());
    assert_eq!(winner(&ready(), &[r]).unwrap(), "plangraph:PATCH-PR");
    let item: Task = serde_json::from_value(json!({"key":"plangraph:A","files":null})).unwrap();
    assert!(item.files.is_empty());
}

#[test]
fn ages_compare_actual_instants_and_unknown_ages_sort_last() {
    let v = json!({"items":[
        {"key":"p:A","priority":1,"created_at":"2026-01-01T00:00:00Z"},
        {"key":"p:B","priority":1,"created_at":"2026-01-01T00:30:00+01:00"},
        {"key":"p:C","priority":1,"created_at":"invalid"}
    ]});
    assert_eq!(winner(&v, &[]).unwrap(), "p:B");
}

#[test]
fn invalid_policy_refuses_before_resource_checks() {
    for p in [
        Policy {
            quota_floor_percent: -1.0,
            ..Policy::default()
        },
        Policy {
            facts_max_age_seconds: -1,
            ..Policy::default()
        },
        Policy {
            ram_per_run_bytes: 0,
            ..Policy::default()
        },
    ] {
        assert_eq!(
            admit(&facts(), &p),
            Admission::Refuse("invalid_policy".into())
        );
    }
}

#[test]
fn shipped_policy_preserves_host_caps_and_admission_floors() {
    let p: Policy = toml::from_str(include_str!("../../../deploy/feeder/policy.toml")).unwrap();
    assert_eq!(p.max_writers["dell"], 5);
    assert_eq!(p.max_writers["coding"], 3);
    assert_eq!(p.disk_floor_bytes, 10_000_000_000);
    assert_eq!(p.ram_per_run_bytes, 4_000_000_000);
    assert_eq!(p.quota_floor_percent, 10.0);
    assert_eq!(
        admit(
            &HostFacts {
                host: "coding.example".into(),
                running_writers: 3,
                ..facts()
            },
            &p
        ),
        Admission::Refuse("max_writers".into())
    );
    // A private full-name override wins over its public short alias.
    let mut p = p;
    p.max_writers.insert("coding.example".into(), 1);
    assert_eq!(
        admit(
            &HostFacts {
                host: "coding.example".into(),
                running_writers: 1,
                ..facts()
            },
            &p
        ),
        Admission::Refuse("max_writers".into())
    );
}
