use super::go_verify::*;
use super::main_state::{Blocking, State};

const BASE: &str = "b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0";
const HEAD: &str = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1";
const TREE: &str = "7777777777777777777777777777777777777777";

fn pins() -> Pins {
    Pins::protected().unwrap()
}

fn jobs(pins: &Pins, tier: &str, attempt: u32) -> Vec<GoJob> {
    pins.matrix.tiers[tier]
        .keys()
        .map(|name| GoJob {
            name: name.clone(),
            conclusion: Some("success".into()),
            attempt,
        })
        .collect()
}

/// The evidence of a green train: every part of it agrees on repository, pull request, base, head, tree,
/// manifest, run and attempt.
fn green() -> Evidence {
    let p = pins();
    Evidence {
        repo: "owner/name".into(),
        pr: 9,
        base: BASE.into(),
        head: HEAD.into(),
        tree: TREE.into(),
        manifest: "m1".into(),
        observed: Observed {
            repo: "owner/name".into(),
            pr: 9,
            pr_head: HEAD.into(),
            pr_base: BASE.into(),
            main_tip: BASE.into(),
        },
        replay: Replay {
            verified: true,
            base: BASE.into(),
            train_sha: HEAD.into(),
            manifest: "m1".into(),
        },
        ci: Ci {
            repo: "owner/name".into(),
            workflow: WORKFLOW_PATH.into(),
            workflow_sha256: p.workflow_sha256.clone(),
            event: EVENT.into(),
            run_id: 5,
            attempt: 2,
            latest_attempt: 2,
            head: HEAD.into(),
            tree: TREE.into(),
            tier: "self".into(),
            jobs: jobs(&p, "self", 2),
        },
        approvals: vec![Approval {
            d_id: "D-100".into(),
            repo: "owner/name".into(),
            pr: 9,
            head: HEAD.into(),
        }],
        delta: Some(DeltaAcceptance {
            run_id: format!("run-{}", "ab".repeat(16)),
            head: HEAD.into(),
            manifest: "m1".into(),
            accepted: true,
        }),
        changed_paths: vec!["rust/plangraph-cli/src/lib.rs".into(), "docs/x.md".into()],
        grants: vec![],
        main: State::Green,
    }
}

fn codes(e: &Evidence) -> Vec<&'static str> {
    verify(e, &pins()).iter().map(|r| r.code).collect()
}

#[test]
fn a_green_train_is_accepted() {
    assert_eq!(codes(&green()), Vec::<&str>::new());
    let hosted = {
        let mut e = green();
        e.ci.tier = "hosted".into();
        e.ci.jobs = jobs(&pins(), "hosted", 2);
        e
    };
    assert_eq!(codes(&hosted), Vec::<&str>::new());
}

#[test]
fn a_moved_head_a_stale_base_and_another_repository_are_refused() {
    let mut e = green();
    e.observed.pr_head = "c".repeat(40);
    assert_eq!(codes(&e), ["moved_head"]);
    let mut e = green();
    e.observed.main_tip = "d".repeat(40);
    assert_eq!(codes(&e), ["stale_base"]);
    let mut e = green();
    e.observed.repo = "other/name".into();
    assert_eq!(codes(&e), ["wrong_repository"]);
    let mut e = green();
    e.replay.manifest = "m2".into();
    assert_eq!(codes(&e), ["replay_mismatch"]);
}

#[test]
fn a_skipped_windows_job_and_a_missing_shard_are_refused_by_name() {
    let windows = "Rust spawner and worker core tests on Windows";
    let mut e = green();
    e.ci.jobs
        .iter_mut()
        .find(|j| j.name == windows)
        .unwrap()
        .conclusion = Some("skipped".into());
    let r = verify(&e, &pins());
    assert_eq!(
        (r[0].code, r[0].detail.as_str()),
        ("job_not_green", windows)
    );
    let shard = "Python tests, SQLite keeper (shard 3 of 4)";
    let mut e = green();
    e.ci.jobs.retain(|j| j.name != shard);
    let r = verify(&e, &pins());
    assert_eq!((r[0].code, r[0].detail.as_str()), ("job_missing", shard));
    // The three-shard hosted matrix does not satisfy the self-hosted tier.
    let mut e = green();
    e.ci.jobs = jobs(&pins(), "hosted", 2);
    assert!(codes(&e).contains(&"job_missing"));
}

#[test]
fn a_mixed_or_superseded_attempt_is_refused() {
    let mut e = green();
    e.ci.jobs[0].attempt = 1;
    assert_eq!(codes(&e), ["mixed_attempt"]);
    let mut e = green();
    e.ci.latest_attempt = 3;
    assert_eq!(codes(&e), ["stale_attempt"]);
}

#[test]
fn the_workflow_event_and_binding_are_pinned() {
    let mut e = green();
    e.ci.workflow_sha256 = "0".repeat(64);
    assert_eq!(codes(&e), ["workflow_unpinned"]);
    let mut e = green();
    e.ci.event = "push".into();
    assert_eq!(codes(&e), ["workflow_unpinned"]);
    let mut e = green();
    e.ci.tree = "e".repeat(40);
    assert_eq!(codes(&e), ["ci_unbound"]);
    let mut e = green();
    e.ci.tier = "fast".into();
    assert_eq!(codes(&e), ["tier_unknown"]);
}

#[test]
fn approval_and_delta_acceptance_are_bound_to_the_exact_head() {
    let mut e = green();
    e.approvals[0].head = "c".repeat(40);
    assert_eq!(codes(&e), ["approval_missing"]);
    let mut e = green();
    e.approvals[0].d_id = "GO".into();
    assert_eq!(codes(&e), ["approval_missing"]);
    let mut e = green();
    e.delta = None;
    assert_eq!(codes(&e), ["delta_missing"]);
    let mut e = green();
    e.delta.as_mut().unwrap().accepted = false;
    assert_eq!(codes(&e), ["delta_missing"]);
    let mut e = green();
    e.delta.as_mut().unwrap().head = "c".repeat(40);
    assert_eq!(codes(&e), ["delta_unbound"]);
}

#[test]
fn an_operator_class_path_needs_a_grant_for_this_head() {
    for path in [
        "tools/ci_commit_gate.py",
        ".github/workflows/tests.yml",
        "tools/gate_wiring_check.py",
        "sub/dir/.gitattributes",
        "deploy/ci-watch/matrix.toml",
    ] {
        let mut e = green();
        e.changed_paths.push(path.into());
        assert_eq!(codes(&e), ["operator_path_unauthorized"], "{path}");
        e.grants.push(Grant {
            d_id: "D-7".into(),
            prefix: path.into(),
            head: HEAD.into(),
        });
        assert!(codes(&e).is_empty(), "{path}");
        e.grants[0].head = "c".repeat(40);
        assert_eq!(
            codes(&e),
            ["operator_path_unauthorized"],
            "{path}: grant of another head"
        );
    }
    let mut e = green();
    e.changed_paths.push(".github/workflows/x.yml".into());
    e.grants.push(Grant {
        d_id: "D-7".into(),
        prefix: ".github/workflows/".into(),
        head: HEAD.into(),
    });
    assert!(codes(&e).is_empty());
    e.grants[0].d_id = "ok".into();
    assert_eq!(codes(&e), ["operator_path_unauthorized"]);
}

#[test]
fn a_red_or_pending_main_is_refused_and_every_reason_is_reported() {
    let mut e = green();
    e.main = State::Red {
        blocking: vec![Blocking {
            run: 1,
            event: "push".into(),
            attempt: 1,
            stalled: false,
            failures: vec![],
        }],
    };
    assert_eq!(codes(&e), ["main_not_green"]);
    e.main = State::Pending;
    e.delta = None;
    e.observed.pr_head = "c".repeat(40);
    assert_eq!(codes(&e), ["moved_head", "delta_missing", "main_not_green"]);
}

#[test]
fn the_pinned_workflow_digest_is_the_digest_of_the_tests_workflow() {
    let text = std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../.github/workflows/tests.yml"
    ))
    .unwrap();
    use sha2::{Digest, Sha256};
    assert_eq!(
        pins().workflow_sha256,
        format!("{:x}", Sha256::digest(text.as_bytes()))
    );
}

#[test]
fn the_cli_prints_one_line_for_green_evidence_and_every_refusal_otherwise() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("e.json");
    let flags = |p: &std::path::Path| {
        std::collections::BTreeMap::from([("evidence".to_string(), p.display().to_string())])
    };
    std::fs::write(&file, serde_json::to_vec(&green()).unwrap()).unwrap();
    let line = super::go_verify::run_cli(&flags(&file)).unwrap();
    assert!(line[0].starts_with("GO-VERIFIED owner/name #9 base "));
    let mut e = green();
    e.delta = None;
    e.observed.main_tip = "d".repeat(40);
    std::fs::write(&file, serde_json::to_vec(&e).unwrap()).unwrap();
    let err = super::go_verify::run_cli(&flags(&file)).unwrap_err();
    assert!(
        err.contains("stale_base") && err.contains("delta_missing"),
        "{err}"
    );
}
