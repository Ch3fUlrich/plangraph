use super::build::{build, BuildInput};
use super::build_tests::{g, part, Fx};
use super::merge_body::*;
use std::path::Path;

/// One commit on a detached branch from `from`, carrying `Plan-Item` trailers.
fn keyed(fx: &Fx, from: &str, file: &str, keys: &[&str]) -> String {
    let p = fx.path();
    g(p, &["checkout", "-q", "--detach", from]);
    std::fs::write(p.join(file), "x\n").unwrap();
    g(p, &["add", "-A"]);
    let trailers: String = keys.iter().map(|k| format!("Plan-Item: {k}\n")).collect();
    g(
        p,
        &["commit", "-q", "-m", &format!("edit {file}\n\n{trailers}")],
    );
    let head = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "main"]);
    head
}

fn two_part_train(fx: &Fx) -> super::train::Manifest {
    let a = keyed(fx, &fx.base(), "a.txt", &["demo:A"]);
    let b = keyed(fx, &fx.base(), "b.txt", &["demo:B", "demo:C"]);
    let input: BuildInput = fx.input(vec![part(1, &a, None), part(2, &b, None)], "", None);
    build(&input).unwrap().manifest.unwrap()
}

#[test]
fn a_two_part_train_body_has_a_review_run_per_item_and_part_matching_the_keepers_reader() {
    let fx = Fx::new();
    let m = two_part_train(&fx);
    let body = merge_body(fx.path(), &m).unwrap();
    let (a, b) = (&m.parts[0].head, &m.parts[1].head);
    assert_eq!(
        body,
        format!(
            "Plan-Item: demo:A\nPlan-Item: demo:B\nPlan-Item: demo:C\n\
             Review-Run: demo:A run-1 {a}\nReview-Run: demo:B run-2 {b}\nReview-Run: demo:C run-2 {b}"
        )
    );
    // The reader the close uses: every Review-Run line matches `_REVIEW_RUN` in full.
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let script = "import sys; sys.path.insert(0, sys.argv[1])\n\
        from plangraph.keeper.store_runs import _REVIEW_RUN\n\
        for line in sys.stdin.read().splitlines():\n\
        \x20   if line.startswith('Review-Run:'):\n\
        \x20       m = _REVIEW_RUN.fullmatch(line)\n\
        \x20       print(*(m.groups() if m else ('NOMATCH', line)))";
    let python = std::env::var("PG_TEST_PYTHON").unwrap_or_else(|_| "python3".into());
    let mut child = std::process::Command::new(python)
        .args(["-I", "-c", script])
        .arg(root.join("src"))
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    use std::io::Write;
    child
        .stdin
        .take()
        .unwrap()
        .write_all(body.as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    assert_eq!(
        String::from_utf8_lossy(&out.stdout),
        format!("demo:A run-1 {a}\ndemo:B run-2 {b}\ndemo:C run-2 {b}\n")
    );
}

#[test]
fn a_body_without_a_review_run_for_an_item_is_refused() {
    let fx = Fx::new();
    let m = two_part_train(&fx);
    let parts = part_keys(fx.path(), &m).unwrap();
    let body = merge_body(fx.path(), &m).unwrap();
    assert!(require_review_runs(&body, &parts).is_ok());
    let without: String = body
        .lines()
        .filter(|l| !l.starts_with("Review-Run: demo:C"))
        .collect::<Vec<_>>()
        .join("\n");
    let refused = require_review_runs(&without, &parts).unwrap_err();
    assert!(refused.contains("Review-Run: demo:C run-2"), "{refused}");
    // Review-Run lines with another part head do not stand in for the part's own.
    let wrong = body.replace(&m.parts[1].head, &m.parts[0].head);
    assert!(require_review_runs(&wrong, &parts).is_err());
    assert!(require_review_runs("", &parts).is_err());
}

#[test]
fn a_part_without_a_plan_item_has_no_body() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let input = fx.input(vec![part(1, &a, None)], "", None);
    let m = build(&input).unwrap().manifest.unwrap();
    let err = merge_body(fx.path(), &m).unwrap_err();
    assert!(err.contains("part #1 carries no Plan-Item"), "{err}");
}
