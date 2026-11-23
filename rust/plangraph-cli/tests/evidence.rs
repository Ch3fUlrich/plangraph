use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const ARTICLE: &str = include_str!("fixtures/evidence/article.md");
const CLAIMS: &str = include_str!("fixtures/evidence/claims.jsonl");
const FIRST: &str = "2026-01-01-queue";
const SECOND: &str = "2026-01-02-queue";

fn article(root: &Path, name: &str) -> PathBuf {
    let dir = root.join(name);
    fs::create_dir_all(dir.join("data/raw")).unwrap();
    fs::write(dir.join("article.md"), ARTICLE).unwrap();
    fs::write(dir.join("claims.jsonl"), CLAIMS).unwrap();
    fs::write(
        dir.join("data/raw/sample.csv"),
        include_str!("fixtures/evidence/sample.csv"),
    )
    .unwrap();
    dir
}

fn evidence(action: &str, root: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(["evidence", action])
        .arg(root)
        .output()
        .unwrap()
}

fn succeeds(action: &str, root: &Path) {
    let output = evidence(action, root);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn refuses(action: &str, root: &Path, details: &[&str]) {
    let output = evidence(action, root);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let reason = String::from_utf8(output.stderr).unwrap();
    for detail in details {
        assert!(reason.contains(detail), "{reason}");
    }
}

#[test]
fn index_has_one_ordered_row_per_result_and_check_is_read_only() {
    let root = tempfile::tempdir().unwrap();
    let second = article(root.path(), SECOND);
    fs::write(
        second.join("article.md"),
        ARTICLE.replace("EV-0001", "EV-0002"),
    )
    .unwrap();
    article(root.path(), FIRST);
    let index = root.path().join("index.jsonl");
    fs::write(&index, "previous index").unwrap();
    succeeds("check", root.path());
    assert_eq!(fs::read_to_string(&index).unwrap(), "previous index");

    succeeds("index", root.path());
    let generated = fs::read_to_string(&index).unwrap();
    let rows: Vec<Value> = generated
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(rows.len(), 4);
    assert_eq!(
        rows[0],
        json!({
            "path": format!("evidence/{FIRST}/article.md"),
            "id": "EV-0001",
            "title": "Fixture: a shorter queue",
            "date": "2026-01-01",
            "status": "accepted",
            "evidence_class": "prospective",
            "components": ["queue"],
            "superseded_by": null,
            "result": 1,
            "metric": "wait",
            "unit": "seconds",
            "baseline": {"value": 10, "window": "before", "n": 2},
            "after": {"value": 8, "window": "after", "n": 2},
            "effect": {"absolute": -2, "relative": "-20 %"},
            "certainty": "low",
            "claim_id": "C01"
        })
    );
    assert_eq!(rows[1]["result"], 2);
    assert_eq!(rows[1]["baseline"], Value::Null);
    assert_eq!(rows[2]["id"], "EV-0002");
    assert_eq!(rows[2]["result"], 1);

    succeeds("index", root.path());
    assert_eq!(fs::read_to_string(index).unwrap(), generated);
}

#[test]
fn every_required_field_is_checked_at_its_schema_location() {
    let root = tempfile::tempdir().unwrap();
    let dir = article(root.path(), FIRST);
    for (field, prefix) in [
        ("title", "title:"),
        ("date", "date:"),
        ("status", "status:"),
        ("superseded_by", "superseded_by:"),
        ("results", "results:"),
        ("baseline", "    baseline: {"),
        ("effect", "    effect: {"),
        ("certainty", "    certainty: low"),
    ] {
        let missing = if field == "results" {
            format!("{}---\n", ARTICLE.split_once("results:").unwrap().0)
        } else {
            ARTICLE
                .lines()
                .filter(|line| !line.starts_with(prefix))
                .collect::<Vec<_>>()
                .join("\n")
        };
        fs::write(dir.join("article.md"), missing).unwrap();
        refuses("check", root.path(), &[FIRST, "article.md", field]);
    }
}

#[test]
fn broken_claims_refuse_an_index_without_replacing_the_previous_one() {
    let root = tempfile::tempdir().unwrap();
    article(root.path(), FIRST);
    let dir = article(root.path(), SECOND);
    let index = root.path().join("index.jsonl");
    fs::write(&index, "previous index").unwrap();
    fs::remove_file(dir.join("data/raw/sample.csv")).unwrap();

    refuses(
        "index",
        root.path(),
        &[SECOND, "claims.jsonl:1", "C01", "data/raw/sample.csv"],
    );
    assert_eq!(fs::read_to_string(index).unwrap(), "previous index");
}

#[test]
fn a_claim_requires_a_local_regular_file() {
    let root = tempfile::tempdir().unwrap();
    let dir = article(root.path(), FIRST);
    let other = article(root.path(), SECOND);
    for data in [
        Value::Null,
        json!("data/raw"),
        json!(format!("../{SECOND}/data/raw/sample.csv")),
        json!(other.join("data/raw/sample.csv")),
    ] {
        let claim = json!({"claim_id": "C01", "data": data});
        fs::write(dir.join("claims.jsonl"), format!("{claim}\n{CLAIMS}")).unwrap();
        refuses(
            "check",
            root.path(),
            &[FIRST, "claims.jsonl:1", "C01", "data"],
        );
    }
}

#[cfg(unix)]
#[test]
fn symlinked_data_cannot_point_outside_the_article() {
    let root = tempfile::tempdir().unwrap();
    let dir = article(root.path(), FIRST);
    let other = article(root.path(), SECOND);
    let data = dir.join("data/raw/sample.csv");
    fs::remove_file(&data).unwrap();
    std::os::unix::fs::symlink(other.join("data/raw/sample.csv"), &data).unwrap();
    refuses("check", root.path(), &[FIRST, "claims.jsonl:1", "C01"]);
}

#[test]
fn unknown_claims_in_prose_and_results_are_refused() {
    let root = tempfile::tempdir().unwrap();
    let dir = article(root.path(), FIRST);
    for (from, to) in [
        ("[C01, C02]", "[C01, C99, a caveat]"),
        ("claim_id: C02", "claim_id: C99"),
    ] {
        fs::write(dir.join("article.md"), ARTICLE.replace(from, to)).unwrap();
        refuses("check", root.path(), &[FIRST, "article.md", "C99"]);
    }
}

#[test]
fn an_article_directory_without_an_article_is_reported() {
    let root = tempfile::tempdir().unwrap();
    fs::create_dir(root.path().join(FIRST)).unwrap();
    refuses("check", root.path(), &[FIRST, "article.md"]);
}
