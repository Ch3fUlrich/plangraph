use super::accept::{parse, require, DEFAULT_SINGLE_SEAT};
use super::train::{PartIn, PartReceipt};

const OLD: &str = "1111111111111111111111111111111111111111";
const NEW: &str = "2222222222222222222222222222222222222222";
const RUN: &str = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_RUN: &str = "run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn answer(leg: &str, run: &str, head: &str, verdict: &str) -> String {
    format!(
        "[served by {leg} via spawner run {run}, status completed]\nVERDICT: {verdict}\n\nFILES READ: src/a.rs\n\nHEAD: {head}\nVERDICT: {verdict}\n"
    )
}

fn part(pr: u64, head: &str, run: &str) -> PartIn {
    PartIn {
        pr,
        head: head.into(),
        parent: None,
        receipt: PartReceipt {
            run_id: run.into(),
            head: head.into(),
        },
    }
}

fn single() -> Vec<String> {
    vec![DEFAULT_SINGLE_SEAT.into()]
}

fn dir_with(files: &[(&str, String)]) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    for (name, text) in files {
        std::fs::write(dir.path().join(name), text).unwrap();
    }
    dir
}

#[test]
fn a_single_seat_leg_accept_bound_to_the_receipt_run_counts() {
    let dir = dir_with(&[(
        "h.txt",
        answer("agy-gemini-4-hydrogen-high", RUN, NEW, "ACCEPT"),
    )]);
    let lines = require(dir.path(), &[part(7, NEW, RUN)], &single()).unwrap();
    assert_eq!(
        lines,
        [format!("accept #7 head {NEW} run {RUN} file h.txt")]
    );
}

#[test]
fn the_deepseek_and_glm_pair_counts_and_one_of_them_alone_does_not() {
    let deepseek = answer("opencode-go-deepseek-v4-flash", RUN, NEW, "ACCEPT");
    let glm = answer("opencode-go-glm-5.3-flash", OTHER_RUN, NEW, "ACCEPT");
    let both = dir_with(&[("d.txt", deepseek.clone()), ("g.txt", glm.clone())]);
    assert!(require(both.path(), &[part(7, NEW, RUN)], &single()).is_ok());
    for (name, text) in [("d.txt", deepseek), ("g.txt", glm)] {
        let one = dir_with(&[(name, text)]);
        let run = if name == "d.txt" { RUN } else { OTHER_RUN };
        let error = require(one.path(), &[part(7, NEW, run)], &single()).unwrap_err();
        assert!(
            error.contains("neither a single-seat leg nor both"),
            "{error}"
        );
    }
}

#[test]
fn an_accept_for_another_run_never_binds_the_part() {
    let dir = dir_with(&[
        (
            "h.txt",
            answer("agy-gemini-4-hydrogen-high", OTHER_RUN, NEW, "ACCEPT"),
        ),
        (
            "x.txt",
            answer("opencode-go-glm-5.3-flash", OTHER_RUN, NEW, "ACCEPT"),
        ),
    ]);
    let error = require(dir.path(), &[part(7, NEW, RUN)], &single()).unwrap_err();
    assert!(
        error.contains("no ACCEPT at this head carries the receipt run"),
        "{error}"
    );
    assert!(error.contains(RUN) && error.contains("part #7"), "{error}");
}

#[test]
fn a_stale_head_is_refused_and_named() {
    let dir = dir_with(&[(
        "p1-r1.txt",
        answer("agy-gemini-4-hydrogen-high", RUN, OLD, "ACCEPT"),
    )]);
    let error = require(dir.path(), &[part(7, NEW, RUN)], &single()).unwrap_err();
    assert!(error.contains(NEW), "{error}");
    assert!(
        error.contains(&format!("p1-r1.txt on {OLD} run {RUN}")),
        "{error}"
    );
}

#[test]
fn request_changes_writer_status_and_unserved_text_never_count() {
    let unserved =
        format!("VERDICT: ACCEPT\n\nFILES READ: src/a.rs\n\nHEAD: {NEW}\nVERDICT: ACCEPT\n");
    let status = format!("p DONE\nHEAD: {NEW}\nREVIEW: none\nVERDICT: ACCEPT\n");
    let dir = dir_with(&[
        (
            "rc.txt",
            answer("agy-gemini-4-hydrogen-high", RUN, NEW, "REQUEST_CHANGES"),
        ),
        ("unserved.txt", unserved),
        ("p.status", status),
        (
            "failed.txt",
            answer("agy-gemini-4-hydrogen-high", RUN, NEW, "ACCEPT").replace("completed", "failed"),
        ),
    ]);
    assert!(require(dir.path(), &[part(7, NEW, RUN)], &single()).is_err());
}

#[test]
fn every_part_needs_its_own_evidence_and_the_single_seat_set_is_configurable() {
    let dir = dir_with(&[("h.txt", answer("cx-gpt-6.2-sol-high", RUN, NEW, "ACCEPT"))]);
    assert!(require(dir.path(), &[part(7, NEW, RUN)], &single()).is_err());
    let gpt = vec!["gpt".to_string()];
    assert!(require(dir.path(), &[part(7, NEW, RUN)], &gpt).is_ok());
    assert!(require(
        dir.path(),
        &[part(7, NEW, RUN), part(8, OLD, OTHER_RUN)],
        &gpt
    )
    .is_err());
}

#[test]
fn only_a_full_head_and_a_run_header_parse() {
    let text = |head: &str| answer("agy-gemini-4-hydrogen-high", RUN, head, "ACCEPT");
    assert!(parse(&text("2222222")).is_none());
    let accept = parse(&text(NEW)).unwrap();
    assert_eq!((accept.head.as_str(), accept.run_id.as_str()), (NEW, RUN));
    assert_eq!(accept.leg, "agy-gemini-4-hydrogen-high");
}
