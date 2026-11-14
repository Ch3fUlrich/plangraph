use pg_ctx::keeper_hygiene::{lint, validate, Disposition};
use serde_json::json;
use std::collections::BTreeSet;

#[test]
fn keeper_hygiene_reports_three_classes_without_mutation() {
    let items = vec![
        json!({"key":"demo:A","state":"open","title":"Same title!","updated_at":"2026-10-01T00:00:00Z","description":"D-404"}),
        json!({"key":"demo:B","state":"claimed","title":"same title","updated_at":"2026-11-14T00:00:00Z"}),
        json!({"key":"demo:C","state":"done","title":"finished","updated_at":"2020-01-01T00:00:00Z","description":"D-405"}),
    ];
    let before = items.clone();
    let issues = lint(&items, &BTreeSet::new(), 1794614400).unwrap();
    assert!(issues
        .iter()
        .any(|i| i.key == "demo:A" && i.class == "stale>30d"));
    assert_eq!(issues.iter().filter(|i| i.class == "title-twin").count(), 2);
    assert!(issues
        .iter()
        .any(|i| i.class == "dead-D-citation" && i.related == vec!["D-404"]));
    assert!(!issues.iter().any(|i| i.key == "demo:C"));
    assert_eq!(items, before);
}

fn clean() -> (serde_json::Value, Disposition, BTreeSet<String>) {
    let item = json!({"key":"demo:A","state":"open","operator":false,"updated_at":"revision-1"});
    let disposition = serde_json::from_value(json!({
        "key":"demo:A", "item_updated_at":"revision-1", "checked_at":2000000,
        "checked_repos":["alpha","beta"], "run_window_start":790400,
        "claims":[],"runs":[],"open_prs":[],"branches":[],"lanes":[],
        "merged_commits":[],"dependents":[],"gate_edges":[],"operator_flags":[],
        "premise_checked":true, "reason":{"kind":"premise_superseded","rule":"9.14"}
    }))
    .unwrap();
    (
        item,
        disposition,
        BTreeSet::from(["alpha".into(), "beta".into()]),
    )
}

#[test]
fn keeper_hygiene_every_disposition_guard_is_required() {
    let (item, disposition, repos) = clean();
    assert!(validate(&item, &disposition, &repos, 2000000).is_ok());
    let raw = serde_json::to_value(&disposition).unwrap();
    for field in [
        "claims",
        "runs",
        "open_prs",
        "branches",
        "lanes",
        "merged_commits",
        "dependents",
        "gate_edges",
        "operator_flags",
    ] {
        let mut changed = raw.clone();
        changed[field] = json!(["present"]);
        let changed = serde_json::from_value(changed).unwrap();
        assert!(
            validate(&item, &changed, &repos, 2000000).is_err(),
            "{field}"
        );
    }
    let mut missing = raw.clone();
    missing.as_object_mut().unwrap().remove("open_prs");
    assert!(serde_json::from_value::<Disposition>(missing).is_err());
    let mut unknown = disposition.clone();
    unknown.checked_repos.remove("beta");
    assert!(validate(&item, &unknown, &repos, 2000000).is_err());
}

#[test]
fn keeper_hygiene_stale_binding_protection_and_twin_guards() {
    let (item, disposition, repos) = clean();
    let raw = serde_json::to_value(&disposition).unwrap();
    for (field, value) in [
        ("checked_at", json!(1)),
        ("run_window_start", json!(1999999)),
        ("premise_checked", json!(false)),
        ("item_updated_at", json!("old")),
        ("key", json!("demo:B")),
        (
            "reason",
            json!({"kind":"premise_superseded","rule":"vague"}),
        ),
        (
            "reason",
            json!({"kind":"twin","key":"demo:B","dc_covers":true,"unique_dc_moved":false}),
        ),
    ] {
        let mut changed = raw.clone();
        changed[field] = value;
        let changed = serde_json::from_value(changed).unwrap();
        assert!(
            validate(&item, &changed, &repos, 2000000).is_err(),
            "{field}"
        );
    }
    for (field, value) in [("operator", json!(true)), ("state", json!("claimed"))] {
        let mut changed = item.clone();
        changed[field] = value;
        assert!(validate(&changed, &disposition, &repos, 2000000).is_err());
    }
    let mut twin = raw;
    twin["reason"] = json!({"kind":"twin","key":"demo:B","dc_covers":true,"unique_dc_moved":true});
    assert!(validate(
        &item,
        &serde_json::from_value(twin).unwrap(),
        &repos,
        2000000
    )
    .is_ok());
}

#[test]
fn keeper_hygiene_decisions_ignore_examples_and_normalize_citations() {
    use pg_ctx::keeper_hygiene::{decision_ids, decision_rows};
    let rows = decision_rows("```\n| D-1 | demo | example |\n```\n    | D-2 | demo | example |\n| D-3 | demo | CANCEL demo:A; rule 9.14 |\n");
    assert_eq!(rows.len(), 1);
    assert_eq!(
        decision_ids("D-0003 D-3-FU notD-9 D-404"),
        BTreeSet::from(["D-3".into(), "D-404".into()])
    );
}

#[test]
fn keeper_hygiene_batch_requires_routing_and_records_each_disposition() {
    let temp = tempfile::tempdir().unwrap();
    let (item, disposition, repos) = clean();
    let write = |name: &str, bytes: Vec<u8>| {
        let path = temp.path().join(name);
        std::fs::write(&path, bytes).unwrap();
        path.to_string_lossy().into_owned()
    };
    let snapshot = write(
        "snapshot.json",
        serde_json::to_vec(&json!({"items":[item]})).unwrap(),
    );
    let dispositions = write(
        "dispositions.json",
        serde_json::to_vec(&vec![disposition]).unwrap(),
    );
    let repos = write("repos.json", serde_json::to_vec(&repos).unwrap());
    let decisions = write(
        "decisions.md",
        b"| D-6000 | demo | CANCEL demo:A; rule 9.14 |\n".to_vec(),
    );
    let mut args = vec![
        "cancel-check".into(),
        snapshot,
        dispositions,
        repos,
        decisions.clone(),
        "2000000".into(),
        "D-6000".into(),
    ];
    let approved: serde_json::Value =
        serde_json::from_str(&pg_ctx::hygiene_cli::execute(&args).unwrap()).unwrap();
    assert_eq!(approved["decision_id"], "D-6000");
    assert!(approved["dispositions"]["demo:A"].is_object());
    args[6] = "D-404".into();
    assert!(pg_ctx::hygiene_cli::execute(&args).is_err());
    args[6] = "D-6000".into();
    std::fs::write(
        decisions,
        "| D-6000 | demo | CANCEL demo:OTHER; rule 9.14 |\n",
    )
    .unwrap();
    assert!(pg_ctx::hygiene_cli::execute(&args).is_err());
}
