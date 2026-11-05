use super::*;
fn fixture() -> Value {
    let mut v = json!({"schema_version":1,"observed_at":20,"entries":[]});
    for (role, n) in [("small", 32000), ("large", 900000)] {
        v["entries"].as_array_mut().unwrap().push(json!({
            "identity":{"role":role,"model":role,"harness":"synthetic"},
            "base":{"window":{"Known":n},"unit":"tokens","observed_at":10,
                "expires_at":30,"provenance":"synthetic measurement"},
            "served":[{"model":role,"protocol":"synthetic","family":"fictional",
                "proving_run":{"id":"run-a","observed_at":10,"provenance":"synthetic proof"}}]
        }));
    }
    v
}
fn unknown(out: &Capture, reason: &str) {
    assert_eq!(out.records[0].window, Window::Unknown(reason.into()));
    assert!(out
        .findings
        .iter()
        .any(|f| f.reason == reason && f.source.starts_with(RELATIVE_PATH)));
}
#[test]
fn retained_evidence_is_verbatim() {
    let mut v = fixture();
    v["observed_at"] = json!(10);
    assert_eq!(decode(&serde_json::to_vec(&v).unwrap(), 25).evidence, v);
}
#[test]
fn coverage_carries_proof() {
    let v = fixture();
    let out = serde_json::to_value(validate(v.clone(), 20)).unwrap();
    let mut expected = v["entries"][0]["served"][0].clone();
    expected["window"] = json!({"Known":32000});
    assert_eq!(out["records"][0]["coverage"][0], expected);
}
#[test]
fn identical_duplicates_preserve_windows() {
    let mut v = fixture();
    let duplicate = v["entries"][0].clone();
    v["entries"].as_array_mut().unwrap().push(duplicate);
    let out = validate(v, 20);
    assert_eq!(out.records[0].window, Window::Known(32000));
    assert_eq!(out.findings.len(), 2);
    assert!(out
        .findings
        .iter()
        .all(|f| f.reason == "duplicate_identical"));
    assert_eq!(out.records[2].window, out.records[0].window);
    assert_eq!(out.records[1].window, Window::Known(900000));
}
#[test]
fn unit_precedes_expiry() {
    let mut v = fixture();
    v["entries"][0]["base"]["unit"] = json!("bytes");
    v["entries"][0]["base"]["expires_at"] = json!(15);
    unknown(&validate(v, 20), "invalid_unit");
}
#[test]
fn empty_entries_alarm() {
    let out = validate(
        json!({"schema_version":1,"observed_at":20,"entries":[]}),
        20,
    );
    assert_eq!(out.records.len(), 1);
    assert_eq!(out.records[0].identity, Value::Null);
    unknown(&out, "missing_entries");
}
#[test]
fn missing_served_alarms() {
    for served in [None, Some(json!([]))] {
        let mut v = fixture();
        v["entries"][0].as_object_mut().unwrap().remove("served");
        if let Some(served) = served {
            v["entries"][0]["served"] = served;
        }
        let out = validate(v, 20);
        assert_eq!(
            out.records[0].coverage[0].window,
            Window::Unknown("missing_coverage".into())
        );
        assert_eq!(out.findings[0].reason, "missing_coverage");
        assert_eq!(out.records[1].window, Window::Known(900000));
    }
}
#[test]
fn changelog_is_one_accurate_line() {
    let note = include_str!("../../../changelog.d/FLEET-WINDOW-CATALOG.md");
    assert_eq!(note.lines().count(), 1);
    assert!(note.starts_with("- **FLEET-WINDOW-CATALOG:**"));
    assert!(!note.contains("typed"));
}
#[test]
fn missing_fields_and_declared_unknown() {
    let mut v = fixture();
    v["entries"][0].as_object_mut().unwrap().remove("base");
    unknown(&validate(v, 20), "missing_field");
    let mut v = fixture();
    v["entries"][0]["base"]["window"] = json!({"Unknown":"not measured"});
    let out = validate(v, 20);
    let expected = Window::Unknown("not measured".into());
    assert_eq!(out.records[0].window, expected);
    assert!(out.findings.is_empty());
}
#[test]
fn schema_path_roundtrip() {
    assert_eq!(RELATIVE_PATH, "fleet/window-catalog.v1.json");
    let v = fixture();
    let out = decode(&serde_json::to_vec(&v).unwrap(), 20);
    assert_eq!(out.evidence, v);
    assert_eq!(out.records[0].window, Window::Known(32000));
    assert_eq!(out.records[1].window, Window::Known(900000));
    assert_eq!(out.records[0].coverage[0].window, Window::Known(32000));
    assert!(out.findings.is_empty());
    let restored: Capture = serde_json::from_slice(&serde_json::to_vec(&out).unwrap()).unwrap();
    assert_eq!(restored.evidence, v);
}
#[test]
fn invalid_inputs_are_isolated() {
    unknown(&decode(b"{", 20), "invalid_json");
    unknown(&validate(json!({}), 20), "invalid_schema");
    let cases = [
        ("unit", json!("bytes"), "invalid_unit"),
        ("window", json!({"Known":"32000"}), "invalid_window"),
        ("window", json!({"Known":0}), "invalid_window"),
        ("window", json!({"Known":1.5}), "invalid_window"),
        ("provenance", json!(""), "missing_provenance"),
    ];
    for (key, value, reason) in cases {
        let mut v = fixture();
        v["entries"][0]["base"][key] = value;
        let out = validate(v, 20);
        unknown(&out, reason);
        assert_eq!(out.records[1].window, Window::Known(900000));
        assert_eq!(out.findings[0].identity["role"], "small");
    }
    let mut v = fixture();
    v["schema_version"] = json!(2);
    unknown(&validate(v, 20), "invalid_schema");
    let mut v = fixture();
    let mut other = v["entries"][0].clone();
    other["base"]["window"] = json!({"Known":42});
    v["entries"].as_array_mut().unwrap().push(other);
    let out = validate(v, 20);
    unknown(&out, "duplicate_conflict");
    assert_eq!(out.records[1].window, Window::Known(900000));
    let mut v = fixture();
    v["entries"][0]["served"][0]["proving_run"] = Value::Null;
    let out = validate(v, 20);
    assert_eq!(
        out.records[0].coverage[0].window,
        Window::Unknown("unproved_coverage".into())
    );
    assert_eq!(out.findings[0].reason, "unproved_coverage");
}
#[test]
fn clocks_never_invent_freshness() {
    unknown(&validate(fixture(), 30), "expired");
    unknown(&validate(fixture(), 9), "clock_backwards");
    let mut v = fixture();
    v["entries"][0]["base"]["observed_at"] = json!(21);
    unknown(&validate(v, 20), "future_observation");
    let out = validate(fixture(), 30);
    unknown(
        &validate(serde_json::to_value(out).unwrap(), 20),
        "clock_backwards",
    );
}
#[test]
fn overrides_and_absences() {
    let mut v = fixture();
    v["entries"][0]["override"] = v["entries"][0]["base"].clone();
    v["entries"][0]["override"]["window"] = json!({"Known":700000});
    v["entries"][0]["override"]["provenance"] = json!("explicit override");
    let out = validate(v.clone(), 20);
    assert_eq!(out.records[0].window, Window::Known(700000));
    assert_eq!(out.records[0].selected_source, "override");
    assert_eq!(out.evidence, v);
    v["entries"][0]["override"]["expires_at"] = json!(20);
    unknown(&validate(v.clone(), 20), "expired");
    v["entries"][0]["override"] = Value::Null;
    unknown(&validate(v, 20), "missing_field");
    let mut v = fixture();
    v["entries"][0]["identity"]["model"] = Value::Null;
    unknown(&validate(v, 20), "missing_identity");
    let mut v = fixture();
    v["entries"][0]["served"][0]["family"] = Value::Null;
    assert!(matches!(
        validate(v, 20).records[0].coverage[0].window,
        Window::Unknown(_)
    ));
}
#[test]
fn capture_is_read_only() {
    let d = tempfile::tempdir().unwrap();
    let missing: Capture = serde_json::from_slice(&capture(d.path(), 20).unwrap()).unwrap();
    unknown(&missing, "unreadable_source");
    assert_eq!(std::fs::read_dir(d.path()).unwrap().count(), 0);
    let path = d.path().join(RELATIVE_PATH);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    let bytes = serde_json::to_vec(&fixture()).unwrap();
    std::fs::write(&path, &bytes).unwrap();
    let sentinel = d.path().join("sentinel");
    std::fs::write(&sentinel, b"unchanged").unwrap();
    let out: Capture = serde_json::from_slice(&capture(d.path(), 20).unwrap()).unwrap();
    assert_eq!(out.records[1].window, Window::Known(900000));
    assert_eq!(std::fs::read(path).unwrap(), bytes);
    assert_eq!(std::fs::read(sentinel).unwrap(), b"unchanged");
    assert_eq!(std::fs::read_dir(d.path()).unwrap().count(), 2);
    assert_eq!(
        std::fs::read_dir(d.path().join("fleet")).unwrap().count(),
        1
    );
}
