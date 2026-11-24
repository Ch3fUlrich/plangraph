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
            migration: None,
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
        keeper_schema: KeeperSchema { base: 27, head: 27 },
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
        "rust/plangraph-cli/src/land/go_verify.rs",
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

#[test]
fn the_cli_refuses_a_fixture_with_a_skipped_windows_job() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("e.json");
    let windows = "Rust spawner and worker core tests on Windows";
    let mut e = green();
    e.ci.jobs
        .iter_mut()
        .find(|j| j.name == windows)
        .unwrap()
        .conclusion = Some("skipped".into());
    std::fs::write(&file, serde_json::to_vec(&e).unwrap()).unwrap();
    let flags =
        std::collections::BTreeMap::from([("evidence".to_string(), file.display().to_string())]);
    let err = super::go_verify::run_cli(&flags).unwrap_err();
    assert!(err.starts_with("go refused:"), "{err}");
    assert!(err.contains(&format!("job_not_green: {windows}")), "{err}");
}

#[test]
fn a_keeper_schema_rise_needs_the_matching_migration_on_the_approval_of_this_head() {
    let mut e = green();
    e.keeper_schema.head = 29;
    assert_eq!(codes(&e), ["migration_missing"]);
    let detail = verify(&e, &pins())[0].detail.clone();
    assert!(
        detail.contains("MIGRATION 29") && detail.contains("keeper-restart"),
        "{detail}"
    );
    for wrong in [Some(27), Some(28), Some(30)] {
        e.approvals[0].migration = wrong;
        assert_eq!(codes(&e), ["migration_missing"], "{wrong:?}");
    }
    e.approvals[0].migration = Some(29);
    assert_eq!(codes(&e), Vec::<&str>::new());
    // A field on an approval of another head does not count.
    e.approvals[0].head = "c".repeat(40);
    assert_eq!(codes(&e), ["approval_missing", "migration_missing"]);
}

#[test]
fn a_regressed_schema_is_refused_and_unchanged_needs_no_field() {
    let mut e = green();
    e.keeper_schema.head = 26;
    assert_eq!(codes(&e), ["migration_regressed"]);
    e.keeper_schema.head = 27;
    e.approvals[0].migration = Some(30);
    assert_eq!(codes(&e), Vec::<&str>::new());
}

#[test]
fn evidence_without_the_schema_versions_does_not_parse() {
    let mut value = serde_json::to_value(green()).unwrap();
    value.as_object_mut().unwrap().remove("keeper_schema");
    assert!(serde_json::from_value::<Evidence>(value).is_err());
}

/// The production path of the two fields: the versions come from the pinned revisions of a git fixture and
/// the `migration` from the GO statement of this exact pull request and head, both through
/// `land-train keeper-schema`, and the evidence is then judged by `verify` for that same head.
#[test]
fn collected_schema_evidence_accepts_or_refuses_the_exact_go_row_by_its_migration_field() {
    use super::build_tests::{g, Fx};
    use super::keeper_schema::SCHEMA_PATH;
    let fx = Fx::new();
    let at = |entries: u32, name: &str| {
        let p = fx.path();
        g(p, &["checkout", "-q", "--detach", &fx.base()]);
        let body: Vec<_> = (1..=entries)
            .map(|v| format!("Migration({v}, a, X)"))
            .collect();
        let path = p.join(SCHEMA_PATH);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, format!("MIGRATIONS = ({})\n", body.join(", "))).unwrap();
        g(p, &["add", "-A"]);
        g(p, &["commit", "-q", "-m", name]);
        let sha = g(p, &["rev-parse", "HEAD"]);
        g(p, &["checkout", "-q", "main"]);
        sha
    };
    let (base, head) = (at(2, "two"), at(3, "three"));
    // The evidence of a green train whose head is the fixture head, in every place that binds it.
    let bound = |fields: &serde_json::Value| {
        let mut e = green();
        e.base = base.clone();
        e.head = head.clone();
        e.observed.pr_head = head.clone();
        e.observed.pr_base = base.clone();
        e.observed.main_tip = base.clone();
        e.replay.base = base.clone();
        e.replay.train_sha = head.clone();
        e.ci.head = head.clone();
        e.delta.as_mut().unwrap().head = head.clone();
        e.approvals[0].head = head.clone();
        e.keeper_schema = serde_json::from_value(fields["keeper_schema"].clone()).unwrap();
        e.approvals[0].migration = serde_json::from_value(fields["migration"].clone()).unwrap();
        e
    };
    let collect = |statement: &str| {
        let flags = std::collections::BTreeMap::from([
            ("repo".to_string(), fx.path().display().to_string()),
            ("base".to_string(), base.clone()),
            ("head".to_string(), head.clone()),
            ("go-statement".to_string(), statement.to_string()),
            ("pr".to_string(), "9".to_string()),
            ("slug".to_string(), "owner/name".to_string()),
        ]);
        super::keeper_schema::run_cli(&flags)
    };
    let verdict = |statement: &str| {
        let line = collect(statement)?.remove(0);
        let fields: serde_json::Value = serde_json::from_str(&line).unwrap();
        Ok::<_, String>(codes(&bound(&fields)))
    };
    let row = |tail: &str| format!("GO owner/name PR #9 GO-HEAD {head}{tail}");
    assert_eq!(verdict(&row("; MIGRATION 3; PLAN x")), Ok(vec![]));
    assert_eq!(
        verdict(&row("; MIGRATION 2")),
        Ok(vec!["migration_missing"])
    );
    assert_eq!(verdict(&row("")), Ok(vec!["migration_missing"]));
    assert_eq!(
        verdict(&row("; MIGRATION +3")).map_err(|e| e.contains("malformed")),
        Err(true)
    );
    // A GO row for another head or pull request supplies no evidence at all.
    let other = format!(
        "GO owner/name PR #9 GO-HEAD {}; MIGRATION 3",
        "c".repeat(40)
    );
    assert!(collect(&other).unwrap_err().contains("not #9 at"));
    let wrong_pr = format!("GO owner/name PR #10 GO-HEAD {head}; MIGRATION 3");
    assert!(collect(&wrong_pr).is_err());
}
