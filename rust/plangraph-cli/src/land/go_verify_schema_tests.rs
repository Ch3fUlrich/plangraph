//! The keeper-schema refusals of `go_verify` and the collected-evidence path (LE-KEEPER-SCHEMA-TRAIN-LANDS-WITHOUT-MIGRATION-FIELD).
use super::go_verify::*;
use super::go_verify_tests::{codes, green, pins};

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
