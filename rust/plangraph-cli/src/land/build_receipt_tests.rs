use super::*;
use serde_json::json;

#[test]
fn train_requires_every_part_receipt_for_its_exact_head() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let b = fx.commit(&fx.base(), "b.txt", "b\n");
    let first = json!({"run_id":format!("run-{}", "a".repeat(32)), "head":a});
    let second = json!({"run_id":format!("run-{}", "b".repeat(32)), "head":b});
    let mut selection = json!([
        {"pr":1,"head":a,"parent":null,"receipt":first},
        {"pr":2,"head":b,"parent":null,"receipt":second},
    ]);
    let valid: Vec<PartIn> = serde_json::from_value(selection.clone()).unwrap();
    selection[1]["receipt"]["head"] = json!(a);
    let outdated = serde_json::from_value(selection).unwrap();
    let error = build(&fx.input(outdated, "", None))
        .err()
        .expect("stale receipt refused");
    assert!(error.contains("receipt"), "{error}");
    let manifest = build(&fx.input(valid.clone(), "", None))
        .unwrap()
        .manifest
        .unwrap();
    let frozen = serde_json::to_value(&manifest).unwrap();
    assert_eq!(frozen["parts"][0]["receipt"], first);
    assert_eq!(frozen["parts"][1]["receipt"], second);
    let mut forged = frozen;
    forged["parts"][1]["receipt"]["run_id"] = first["run_id"].clone();
    let forged = serde_json::from_value(forged).unwrap();
    assert!(verify_manifest(fx.path(), &forged, &valid)
        .unwrap_err()
        .contains("registered"));
}

#[test]
fn completed_build_warns_with_the_paths_and_parts_that_overlap() {
    use super::super::cli::{build_train, Build};
    use super::super::main_state::State;

    let fx = Fx::new();
    let base = fx.base();
    let first = fx.commit(
        &base,
        "shared.txt",
        "first\nline 2\nline 3\nline 4\nline 5\nline 6\n",
    );
    let second = fx.commit(
        &base,
        "shared.txt",
        "line 1\nline 2\nline 3\nline 4\nline 5\nsecond\n",
    );
    let out = tempfile::tempdir().unwrap();
    let job = Build {
        repo: fx.path().to_owned(),
        base: base.clone(),
        name: "fixture".into(),
        parts: vec![part(10, &first, None), part(20, &second, None)],
        attributes: String::new(),
        cap: None,
        out: out.path().to_owned(),
        branch: "land/train-fixture".into(),
        resolutions: vec![],
        protected: vec![],
        open: BuildOpen {
            build: "fixture".into(),
            policy_version: git::POLICY_VERSION.into(),
            base: base.clone(),
            grant_digest: "fixture".into(),
            snapshot_position: 1,
        },
    };
    let lines = build_train(&job, &State::Green, &base).unwrap();
    assert!(lines[0].contains("2 merged"), "{lines:?}");
    assert!(
        lines.iter().any(|line| line.contains("shared.txt")
            && line.contains("#10")
            && line.contains("#20")),
        "{lines:?}"
    );
}
