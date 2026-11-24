use super::accept::{parse, require};
use super::train::{PartIn, PartReceipt};

const OLD: &str = "1111111111111111111111111111111111111111";
const NEW: &str = "2222222222222222222222222222222222222222";

fn review(head: &str, verdict: &str) -> String {
    format!("VERDICT: {verdict}\n\nFILES READ: src/a.rs\n\nHEAD: {head}\nVERDICT: {verdict}\n")
}

fn part(pr: u64, head: &str) -> PartIn {
    PartIn {
        pr,
        head: head.into(),
        parent: None,
        receipt: PartReceipt {
            run_id: format!("run-{}", "a".repeat(32)),
            head: head.into(),
        },
    }
}

#[test]
fn part_with_accept_at_its_head_passes() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("p1.accept.txt"), review(NEW, "ACCEPT")).unwrap();
    let lines = require(dir.path(), &[part(7, NEW)]).unwrap();
    assert_eq!(lines, [format!("accept #7 head {NEW} file p1.accept.txt")]);
}

#[test]
fn part_whose_last_accept_is_on_an_older_head_is_refused_by_name() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("p1-r1.accept.txt"), review(OLD, "ACCEPT")).unwrap();
    let error = require(dir.path(), &[part(7, NEW)]).unwrap_err();
    assert!(error.contains("part #7"), "{error}");
    assert!(error.contains(NEW), "{error}");
    assert!(error.contains("ACCEPT file"), "{error}");
    assert!(
        error.contains(&format!("p1-r1.accept.txt on {OLD}")),
        "{error}"
    );
}

#[test]
fn request_changes_and_writer_status_never_count() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("rc.txt"), review(NEW, "REQUEST_CHANGES")).unwrap();
    let status = format!("p DONE\nHEAD: {NEW}\nREVIEW: none\nVERDICT: ACCEPT\n");
    std::fs::write(dir.path().join("p.status"), status).unwrap();
    assert!(require(dir.path(), &[part(7, NEW)]).is_err());
}

#[test]
fn only_a_full_head_counts() {
    assert!(parse(&review("2222222", "ACCEPT")).is_none());
    assert_eq!(parse(&review(NEW, "ACCEPT")).unwrap().head, NEW);
}
