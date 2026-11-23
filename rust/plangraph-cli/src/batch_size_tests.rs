use super::*;

fn writer(intelligence: Option<f64>, window: Option<u64>) -> Writer {
    Writer {
        aa_intelligence: intelligence,
        context_window: window,
    }
}
fn item(key: &str, kind: &str, bytes: Option<u64>) -> Item {
    Item {
        key: key.into(),
        kind: kind.into(),
        expected_bytes: bytes,
        text: Some(format!("Why: {key}\nHow: h\nDone when: d")),
        files: Vec::new(),
        design_ref: None,
    }
}
fn input(writer: Writer, sizes: &[u64]) -> Input {
    Input {
        writer,
        items: sizes
            .iter()
            .enumerate()
            .map(|(i, b)| item(&format!("k{i}"), "lane", Some(*b)))
            .collect(),
        design_roots: BTreeMap::new(),
        prior_part_files: Vec::new(),
        merged_bytes: BTreeMap::new(),
        limit_bytes: None,
        last: None,
        run_values: BTreeMap::new(),
    }
}

#[test]
fn model_cap_is_10_5_or_3() {
    assert_eq!(model_cap(&writer(Some(60.0), Some(400_000))).0, 10);
    assert_eq!(model_cap(&writer(Some(50.0), Some(1_000_000))).0, 10);
    assert_eq!(model_cap(&writer(Some(60.0), Some(399_999))).0, 5);
    assert_eq!(model_cap(&writer(Some(60.0), None)).0, 5);
    assert_eq!(model_cap(&writer(Some(49.9), Some(1_000_000))).0, 3);
    assert_eq!(model_cap(&writer(None, Some(1_000_000))).0, 3);
}

#[test]
fn small_items_are_limited_by_the_model_cap() {
    let sizes = [1_000; 12];
    assert_eq!(
        plan(&input(writer(Some(60.0), Some(500_000)), &sizes)).count,
        10
    );
    assert_eq!(
        plan(&input(writer(Some(60.0), Some(200_000)), &sizes)).count,
        5
    );
    assert_eq!(
        plan(&input(writer(Some(10.0), Some(500_000)), &sizes)).count,
        3
    );
}

#[test]
fn sizes_over_the_planning_target_give_a_smaller_count() {
    // Target is 60% of the 100k limit; 4 x 14k = 56k fits, the fifth passes 60k.
    let p = plan(&input(writer(Some(60.0), Some(500_000)), &[14_000; 8]));
    assert_eq!(p.count, 4);
    assert!(
        p.reason.contains("k4 would pass the target"),
        "{}",
        p.reason
    );
}

#[test]
fn an_item_above_half_the_limit_goes_alone() {
    let first = plan(&input(
        writer(Some(60.0), Some(500_000)),
        &[60_000, 1_000, 1_000],
    ));
    assert_eq!(
        (first.count, first.keys.as_slice()),
        (1, ["k0".to_string()].as_slice())
    );
    let later = plan(&input(
        writer(Some(60.0), Some(500_000)),
        &[1_000, 70_000, 1_000],
    ));
    assert_eq!(later.keys, ["k0", "k2"]);
    assert_eq!(later.alone, ["k1"]);
    // Exactly half still shares.
    assert_eq!(
        plan(&input(writer(Some(60.0), Some(500_000)), &[50_000]))
            .alone
            .len(),
        0
    );
}

#[test]
fn feedback_moves_the_count_by_one() {
    let sizes = [1_000; 12];
    let mut base = input(writer(Some(60.0), Some(500_000)), &sizes);
    base.last = Some(Last {
        count: 6,
        outcome: Outcome::Skipped,
    });
    assert_eq!(plan(&base).count, 5);
    base.last = Some(Last {
        count: 6,
        outcome: Outcome::HighSurvived,
    });
    assert_eq!(plan(&base).count, 5);
    base.last = Some(Last {
        count: 6,
        outcome: Outcome::AcceptedRound1,
    });
    assert_eq!(plan(&base).count, 7);
    base.last = Some(Last {
        count: 6,
        outcome: Outcome::Neutral,
    });
    assert_eq!(plan(&base).count, 6);
    // One more never passes the model cap.
    base.last = Some(Last {
        count: 10,
        outcome: Outcome::AcceptedRound1,
    });
    assert_eq!(plan(&base).count, 10);
    // One fewer never reaches zero.
    base.last = Some(Last {
        count: 1,
        outcome: Outcome::Skipped,
    });
    assert_eq!(plan(&base).count, 1);
}

#[test]
fn missing_estimates_use_the_median_of_merged_items_of_the_kind() {
    let merged: BTreeMap<String, Vec<u64>> = [
        ("lane".to_string(), vec![10_000, 30_000, 20_000]),
        ("goal".to_string(), vec![1_000]),
    ]
    .into();
    assert_eq!(expected_bytes(&item("a", "lane", None), &merged), 20_000);
    assert_eq!(
        expected_bytes(&item("a", "gate", None), &merged),
        median(&[1_000, 10_000, 20_000, 30_000]).unwrap()
    );
    assert_eq!(expected_bytes(&item("a", "lane", Some(5)), &merged), 5);
    assert_eq!(
        expected_bytes(&item("a", "lane", None), &BTreeMap::new()),
        UNKNOWN_ITEM_BYTES
    );
}

#[test]
fn the_reason_names_every_bound_and_the_json_input_parses() {
    let json = r#"{"writer":{"aa_intelligence":60,"context_window":500000},
        "items":[{"key":"a","kind":"lane","expected_bytes":5000}],
        "merged_bytes":{"lane":[1,2,3]}, "last":{"count":3,"outcome":"accepted_round1"}}"#;
    let parsed: Input = serde_json::from_str(json).unwrap();
    let p = plan(&parsed);
    assert_eq!(p.count, 1);
    for part in ["model cap 10", "feedback 4", "fit 5000 of 60000"] {
        assert!(p.reason.contains(part), "{part} in {}", p.reason);
    }
}

fn briefed(sizes: &[u64], writer: Writer) -> (Input, Plan) {
    let mut input = input(writer, sizes);
    input.run_values.insert("NAME".into(), "batch-x".into());
    let plan = plan(&input);
    (input, plan)
}

#[test]
fn the_brief_carries_the_selected_items_and_the_reason_and_waits_with_the_rest() {
    // A writer below the strong threshold: the cap is 3 of 5 items.
    let (input, plan) = briefed(&[1_000; 5], writer(Some(10.0), Some(500_000)));
    let text = brief(&input, &plan).unwrap();
    assert_eq!(plan.count, 3);
    assert!(text.starts_with("# Batch size\n3 of 5 item(s): 3 item(s): model cap 3"));
    assert!(text.contains(&plan.reason));
    for key in ["k0", "k1", "k2"] {
        assert!(text.contains(&format!("\n## {key}\nWhy: {key}\n")), "{key}");
    }
    for key in ["k3", "k4"] {
        assert!(
            !text.contains(&format!("## {key}")),
            "{key} must not be in the brief"
        );
    }
    assert!(text.contains("Left for the next batch: k3 k4\n"));
    // The items come in order, and the spawner recognises the batch shape.
    assert!(text.find("## k0").unwrap() < text.find("## k1").unwrap());
    assert!(text.contains("\n# Items\n") && text.ends_with("\n# Run values\nNAME: batch-x\n"));
}

#[test]
fn an_item_without_text_refuses_the_brief() {
    let (mut input, plan) = briefed(&[1_000; 2], writer(Some(60.0), Some(500_000)));
    input.items[1].text = Some("  ".into());
    assert_eq!(
        brief(&input, &plan).unwrap_err(),
        "item k1 has no text for the brief"
    );
}

#[test]
fn the_command_prints_the_brief_with_dash_dash_brief_and_the_plan_json_without() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("input.json");
    std::fs::write(
        &file,
        serde_json::json!({
            "writer": {"aa_intelligence": 10.0, "context_window": 500000},
            "items": (0..5).map(|i| serde_json::json!({
                "key": format!("k{i}"), "kind": "lane", "expected_bytes": 1000,
                "text": format!("Why: w{i}\nHow: h\nDone when: d")})).collect::<Vec<_>>(),
            "run_values": {"NAME": "batch-x"}
        })
        .to_string(),
    )
    .unwrap();
    let path = file.to_string_lossy().into_owned();
    let mut brief_out = Vec::new();
    assert_eq!(
        execute(&[path.clone(), "--brief".into()], &mut brief_out),
        0
    );
    let text = String::from_utf8(brief_out).unwrap();
    assert!(text.contains("## k2\nWhy: w2") && !text.contains("## k3"));
    assert!(text.contains("model cap 3"));
    let mut plan_out = Vec::new();
    assert_eq!(execute(std::slice::from_ref(&path), &mut plan_out), 0);
    let value: serde_json::Value = serde_json::from_slice(&plan_out).unwrap();
    assert_eq!(value["count"], 3);
    assert_eq!(execute(&[path, "--other".into()], &mut Vec::new()), 2);
}

#[test]
fn a_repeated_item_key_refuses_the_brief_and_the_plan() {
    let (mut input, plan) = briefed(&[1_000; 3], writer(Some(60.0), Some(500_000)));
    input.items[1].key = "k0".into();
    input.items[1].text = None;
    assert_eq!(
        brief(&input, &plan).unwrap_err(),
        "item key k0 is listed twice"
    );
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("input.json");
    let item = |text: Option<&str>| serde_json::json!({"key": "same", "kind": "lane", "expected_bytes": 1000, "text": text});
    std::fs::write(
        &file,
        serde_json::json!({"writer": {"aa_intelligence": 60.0, "context_window": 500000},
            "items": [item(Some("Why: a")), item(None)]})
        .to_string(),
    )
    .unwrap();
    let path = file.to_string_lossy().into_owned();
    for args in [vec![path.clone()], vec![path.clone(), "--brief".into()]] {
        let mut out = Vec::new();
        assert_eq!(execute(&args, &mut out), 2);
        assert!(out.is_empty());
    }
}

struct FullDisk;
impl std::io::Write for FullDisk {
    fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
        Err(std::io::Error::other("no space left on device"))
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

#[test]
fn an_output_that_cannot_be_written_is_a_failure_not_a_success() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("input.json");
    std::fs::write(
        &file,
        serde_json::json!({"writer": {"aa_intelligence": 60.0, "context_window": 500000},
            "items": [{"key": "k0", "kind": "lane", "expected_bytes": 1000, "text": "Why: a"}]})
        .to_string(),
    )
    .unwrap();
    let path = file.to_string_lossy().into_owned();
    assert_eq!(execute(std::slice::from_ref(&path), &mut FullDisk), 2);
    assert_eq!(execute(&[path, "--brief".into()], &mut FullDisk), 2);
}

#[test]
fn a_module_already_planned_in_this_train_waits_for_the_next_train() {
    let fixture = serde_json::json!({
        "writer": {"aa_intelligence": 60.0, "context_window": 500000},
        "items": [
            {"key": "first", "kind": "lane", "expected_bytes": 1000,
             "files": ["tools/merge_batch_land.py"]},
            {"key": "second", "kind": "lane", "expected_bytes": 1000,
             "files": ["tools/merge_batch_land.py"]},
            {"key": "third", "kind": "lane", "expected_bytes": 1000,
             "files": ["docs/fixture.md", "tools/unrelated.py"]}
        ]
    });
    let same_part = plan(&serde_json::from_value(fixture.clone()).unwrap());
    assert_eq!(same_part.keys, ["first", "second", "third"]);

    let mut next_part = fixture;
    next_part["items"].as_array_mut().unwrap().remove(0);
    next_part["prior_part_files"] =
        serde_json::json!(["tools/merge_batch_land.py", "docs/fixture.md"]);
    let same_train = plan(&serde_json::from_value(next_part.clone()).unwrap());
    assert_eq!(same_train.keys, ["third"]);
    assert!(same_train.reason.contains("second"));
    assert!(same_train.reason.contains("tools/merge_batch_land.py"));

    next_part["prior_part_files"] = serde_json::json!([]);
    let following_train = plan(&serde_json::from_value(next_part).unwrap());
    assert_eq!(following_train.keys, ["second", "third"]);
}

#[test]
fn selected_design_files_are_embedded_verbatim_and_missing_refs_name_the_path() {
    let root = tempfile::tempdir().unwrap();
    let design = "  Contract input\r\n\nkeep trailing whitespace  \n";
    std::fs::write(root.path().join("contract.md"), design).unwrap();
    let input: Input = serde_json::from_value(serde_json::json!({
        "writer": {},
        "items": [{"key": "demo:A", "kind": "lane", "text": "How: use the contract",
                   "design_ref": "data:contract.md"}],
        "design_roots": {"data": root.path()}
    }))
    .unwrap();
    let plan = plan(&input);
    assert!(brief(&input, &plan).unwrap().contains(design));
    std::fs::remove_file(root.path().join("contract.md")).unwrap();
    assert!(brief(&input, &plan)
        .unwrap_err()
        .contains("data:contract.md"));
}

#[test]
fn unqualified_design_refs_require_the_items_project_root() {
    let root = tempfile::tempdir().unwrap();
    let design = "The project's design contract\n";
    std::fs::write(root.path().join("Cargo.toml"), design).unwrap();
    assert!(std::path::Path::new("Cargo.toml").is_file());
    let mut input: Input = serde_json::from_value(serde_json::json!({
        "writer": {},
        "items": [{"key": "demo:A", "kind": "lane", "text": "How: use the contract",
                   "design_ref": "Cargo.toml"}],
        "design_roots": {"other": root.path()}
    }))
    .unwrap();
    let plan = plan(&input);
    let error = brief(&input, &plan).unwrap_err();
    assert!(error.contains("Cargo.toml"));
    assert!(error.contains("no design root configured for demo"));

    input
        .design_roots
        .insert("demo".into(), root.path().to_path_buf());
    assert!(brief(&input, &plan).unwrap().contains(design));
}
