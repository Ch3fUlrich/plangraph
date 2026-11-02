use super::*;
use std::collections::BTreeSet;

const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const W: &str = include_str!("../../../../docs/schemas/examples/workspace.writer.json");
const S: &str = include_str!("../../../../docs/schemas/examples/workspace.seat.json");
const R: &str = include_str!("../../../../docs/schemas/examples/workspace.read.json");
const N: &str = include_str!("../../../../docs/schemas/examples/workspace.none.json");
const SCHEMA: &str = include_str!("../../../../docs/schemas/workspace.schema.json");

#[test]
fn examples_parse_and_validate() {
    for (mode, src) in [("writer", W), ("seat", S), ("read", R), ("none", N)] {
        let w = Workspace::from_json_str(src).expect(mode);
        let name = format!("{:?}", w.mode).to_lowercase();
        assert_eq!(name, mode);
    }
}

fn fill(src: &str) -> String {
    let long = format!("lane/{}", "a".repeat(196));
    src.replace("@A", A).replace("@B", B).replace("@L", &long)
}

/// (rule the error must name)|(json); one refusal per rule and mode.
const REFUSE: &[&str] = &[
    r#"start or end|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/"}"#,
    r#"start or end|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/x."}"#,
    r#"start or end|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/x/"}"#,
    r#"start or end|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/x./"}"#,
    r#"base_sha is required|{"repo_id":"p","mode":"writer","revision_sha":"@A","branch":"lane/x"}"#,
    r#"revision_sha is required|{"repo_id":"p","mode":"writer","base_sha":"@A","branch":"lane/x"}"#,
    r#"branch is required|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A"}"#,
    r#"must equal|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@B","branch":"lane/x"}"#,
    r#"`lane/`|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"main"}"#,
    r#"`..`|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/a..b"}"#,
    r#"`//`|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/a//b"}"#,
    r#"`@{`|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/a@{b"}"#,
    "control characters|{\"repo_id\":\"p\",\"mode\":\"writer\",\"base_sha\":\"@A\",\"revision_sha\":\"@A\",\"branch\":\"lane/a\\tb\"}",
    r#"spaces|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/a b"}"#,
    r#"must not contain any of|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/a~b"}"#,
    r#"start or end|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/.x"}"#,
    r#".lock|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/x.lock"}"#,
    r#"200|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"@L"}"#,
    r#"40 lowercase hex|{"repo_id":"p","mode":"writer","base_sha":"deadbeef","revision_sha":"@A","branch":"lane/x"}"#,
    r#"40 lowercase hex|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","branch":"lane/x"}"#,
    r#"40 lowercase hex|{"repo_id":"p","mode":"writer","base_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","revision_sha":"@A","branch":"lane/x"}"#,
    r#"repo_id|{"repo_id":"Repo","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/x"}"#,
    r#"workspace json|{"repo_id":"p","mode":"writer","base_sha":"@A","revision_sha":"@A","branch":"lane/x","extra":1}"#,
    r#"base_sha is required|{"repo_id":"p","mode":"seat","revision_sha":"@A"}"#,
    r#"revision_sha is required|{"repo_id":"p","mode":"seat","base_sha":"@A"}"#,
    r#"must be absent|{"repo_id":"p","mode":"seat","base_sha":"@A","revision_sha":"@A","branch":"lane/x"}"#,
    r#"40 lowercase hex|{"repo_id":"p","mode":"seat","base_sha":"@A","revision_sha":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}"#,
    r#"repo_id|{"repo_id":"r!","mode":"seat","base_sha":"@A","revision_sha":"@A"}"#,
    r#"workspace json|{"repo_id":"p","mode":"seat","base_sha":"@A","revision_sha":"@A","extra":1}"#,
    r#"revision_sha is required|{"repo_id":"p","mode":"read"}"#,
    r#"must be absent|{"repo_id":"p","mode":"read","revision_sha":"@A","branch":"lane/x"}"#,
    r#"40 lowercase hex|{"repo_id":"p","mode":"read","base_sha":"deadbeef","revision_sha":"@A"}"#,
    r#"workspace json|{"repo_id":"p","mode":"read","revision_sha":"@A","extra":1}"#,
    r#"base_sha must be absent|{"repo_id":"p","mode":"none","base_sha":"@A"}"#,
    r#"revision_sha must be absent|{"repo_id":"p","mode":"none","revision_sha":"@A"}"#,
    r#"branch must be absent|{"repo_id":"p","mode":"none","branch":"lane/x"}"#,
    r#"missing field|{"mode":"none"}"#,
    r#"workspace json|{"repo_id":"p","mode":"none","extra":1}"#,
];

#[test]
fn refusals_name_the_field_and_rule() {
    for case in REFUSE {
        let (rule, src) = case.split_once('|').unwrap();
        let e = Workspace::from_json_str(&fill(src)).unwrap_err();
        assert!(e.to_string().contains(rule), "{case}: {e}");
    }
}

#[test]
fn seat_input_diff_maps_revision_to_head() {
    let seat = Workspace::from_json_str(S).unwrap();
    let d = seat.seat_input_diff().unwrap();
    assert_eq!(d.base_sha, seat.base_sha.unwrap());
    assert_eq!(d.head_sha, seat.revision_sha.unwrap());
    assert_eq!(d.repo_id, "plangraph");
    let writer = Workspace::from_json_str(W).unwrap();
    assert!(matches!(
        writer.seat_input_diff(),
        Err(WorkspaceError::NotSeat(Mode::Writer))
    ));
}

#[test]
fn record_validates_shas_and_maps_head_at_end() {
    let good = format!("{{\"repo_id\":\"p\",\"toplevel\":\"t\",\"base_sha\":\"{A}\",\"head_at_start\":\"{A}\",\"head_at_end\":\"{B}\"}}");
    let mut rec: WorkspaceRecord = serde_json::from_str(&good).unwrap();
    assert!(rec.validate().is_ok());
    let d = rec.writer_result_diff().unwrap();
    assert_eq!((d.base_sha.as_str(), d.head_sha.as_str()), (A, B));
    rec.head_at_end = "short".into();
    assert!(matches!(rec.validate(), Err(WorkspaceError::Sha(f, _)) if f == "head_at_end"));
}

#[test]
fn schema_properties_match_serde_fields() {
    let schema: serde_json::Value = serde_json::from_str(SCHEMA).unwrap();
    let mut fields = BTreeSet::new();
    for src in [W, S, R, N] {
        let v = serde_json::to_value(Workspace::from_json_str(src).unwrap()).unwrap();
        fields.extend(v.as_object().unwrap().keys().cloned());
    }
    let props = schema["properties"].as_object().unwrap();
    assert_eq!(props.keys().cloned().collect::<BTreeSet<_>>(), fields);
}

#[test]
fn read_optional_base_and_nulls() {
    let rb = include_str!("../../../../docs/schemas/examples/workspace.read-with-base.json");
    for src in [W, S, R, N, rb] {
        Workspace::from_json_str(src).unwrap();
        for field in ["base_sha", "revision_sha", "branch"] {
            let mut v: serde_json::Value = serde_json::from_str(src).unwrap();
            v[field] = serde_json::Value::Null;
            assert!(Workspace::from_json_str(&v.to_string())
                .unwrap_err()
                .to_string()
                .contains("expected a string"));
        }
    }
}

#[test]
fn branch_ascii_edges_and_boundaries() {
    let mut w = Workspace::from_json_str(W).unwrap();
    for b in ["lane/main".to_string(), format!("lane/{}", "a".repeat(195))] {
        w.branch = Some(b);
        w.validate().unwrap();
    }
    assert_eq!(
        WorkspaceError::BranchNotLane("main".into()).to_string(),
        "writer branch \"main\" must start with `lane/`"
    );
    let mut bad = vec![
        "lane/".into(),
        "lane/x.".into(),
        "lane/x/".into(),
        "lane/x./".into(),
        "lane/a\u{85}b".into(),
        format!("lane/{}", "😀".repeat(200)),
        format!("lane/{}", "é".repeat(195)),
        format!("lane/{}", "a".repeat(196)),
    ];
    for c in ['~', '^', ':', '?', '*', '[', '\\', ' ', '\0', '\u{7f}'] {
        bad.push(format!("lane/a{c}b"));
    }
    for b in bad {
        w.branch = Some(b);
        assert!(w.validate().is_err());
    }
}

#[test]
fn repo_id_boundaries() {
    let mut w = Workspace::from_json_str(N).unwrap();
    w.repo_id = "a".repeat(64);
    w.validate().unwrap();
    for id in ["a".repeat(65), String::new(), "R!".into()] {
        w.repo_id = id;
        assert!(matches!(w.validate(), Err(WorkspaceError::RepoId(_))));
    }
}

#[test]
fn record_validates_before_diff() {
    let good = WorkspaceRecord {
        repo_id: "p".into(),
        toplevel: "t".into(),
        branch: Some("lane/main".into()),
        base_sha: A.into(),
        head_at_start: A.into(),
        head_at_end: B.into(),
    };
    good.writer_result_diff().unwrap();
    let mut absent = good.clone();
    absent.branch = None;
    absent.writer_result_diff().unwrap();
    for field in [
        "repo_id",
        "branch",
        "base_sha",
        "head_at_start",
        "head_at_end",
    ] {
        let mut v = serde_json::to_value(&good).unwrap();
        v[field] = "R!".into();
        let rec: WorkspaceRecord = serde_json::from_value(v).unwrap();
        assert!(rec.validate().is_err(), "{field}");
        assert!(rec.writer_result_diff().is_err(), "{field}");
    }
}
