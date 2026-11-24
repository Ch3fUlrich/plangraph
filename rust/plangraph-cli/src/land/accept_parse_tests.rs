//! How one answer file is read: the launcher header, the head, the verdict, and what an unreadable or
//! contradictory file still owns. The full admission path is exercised through `require`.
use super::accept::{parse, require};
use super::accept_tests::{
    answer, dir_with, gate, part, DEEPSEEK, GLM, HYDROGEN, NEW, OLD, OTHER_RUN, RUN,
};

const MORE: &str = "REQUEST_CHANGES";

fn header(leg: &str, run: &str) -> String {
    format!("[served by {leg} via spawner run {run}, status completed]")
}

#[test]
fn only_a_full_head_and_a_run_header_parse_as_an_accepted_answer() {
    let text = |head: &str| answer(HYDROGEN, RUN, head, "ACCEPT");
    let short = parse(&text("2222222")).unwrap();
    assert!(short.ambiguous && !short.accepted && short.head.is_none());
    let accept = parse(&text(NEW)).unwrap();
    assert_eq!(
        (
            accept.head.as_deref(),
            accept.run_id.as_str(),
            accept.leg.as_str()
        ),
        (Some(NEW), RUN, HYDROGEN)
    );
    assert!(accept.accepted && !accept.ambiguous);
    // Text that does not start with the launcher header owns no run.
    assert!(parse(&format!("review text\n{}", text(NEW))).is_none());
    assert!(parse("VERDICT: ACCEPT\n").is_none());
}

#[test]
fn a_repeated_header_conflicting_heads_or_verdicts_or_a_failed_run_keep_the_run_but_never_accept() {
    let good = answer(HYDROGEN, RUN, NEW, MORE);
    let repeated = format!("{good}{}\n", header(HYDROGEN, OTHER_RUN));
    let conflicting_heads = good.replace("\nFILES READ", &format!("\nHEAD: {OLD}\nFILES READ"));
    let conflicting_verdicts = good.replace("\nFILES READ", "\nVERDICT: ACCEPT\nFILES READ");
    let failed = good.replacen("completed", "failed", 1);
    let no_head = good.replace(&format!("HEAD: {NEW}\n"), "");
    for (name, text) in [
        ("repeated header", repeated),
        ("conflicting heads", conflicting_heads),
        ("conflicting verdicts", conflicting_verdicts),
        ("failed run", failed),
        ("no head", no_head),
    ] {
        let answer = parse(&text).unwrap_or_else(|| panic!("{name}: the run must stay owned"));
        assert_eq!(answer.run_id, RUN, "{name}");
        assert!(answer.ambiguous && !answer.accepted, "{name}");
    }
    // The same ambiguity on an ACCEPT never accepts.
    let accept = answer(HYDROGEN, RUN, NEW, "ACCEPT");
    let both = format!("{accept}{}\n", header(HYDROGEN, OTHER_RUN));
    assert!(!parse(&both).unwrap().accepted);
    // Equal repeated heads are not a conflict.
    let twice = accept.replace("\nFILES READ", &format!("\nHEAD: {NEW}\nFILES READ"));
    assert!(parse(&twice).unwrap().accepted);
}

/// A completed REQUEST_CHANGES of the receipt run whose body repeats the header, for the admission path.
fn ambiguous_rejection(leg: &str, run: &str, flaw: &str) -> String {
    let good = answer(leg, run, NEW, MORE);
    match flaw {
        "repeated-header" => format!("{good}{}\n", header(leg, OTHER_RUN)),
        _ => good.replace("\nFILES READ", &format!("\nHEAD: {OLD}\nFILES READ")),
    }
}

#[test]
fn an_ambiguous_rejection_of_the_receipt_run_blocks_a_single_seat_accept() {
    for flaw in ["repeated-header", "conflicting-heads"] {
        let dir = dir_with(&[
            ("h.txt", answer(HYDROGEN, RUN, NEW, "ACCEPT")),
            ("h-rc.txt", ambiguous_rejection(HYDROGEN, RUN, flaw)),
        ]);
        let error = require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).unwrap_err();
        assert!(error.contains("more than one answer"), "{flaw}: {error}");
    }
}

#[test]
fn an_ambiguous_rejection_of_a_pair_run_blocks_the_pair() {
    for flaw in ["repeated-header", "conflicting-heads"] {
        let dir = dir_with(&[
            ("d.txt", answer(DEEPSEEK, RUN, NEW, "ACCEPT")),
            ("g.txt", answer(GLM, OTHER_RUN, NEW, "ACCEPT")),
            ("g-rc.txt", ambiguous_rejection(GLM, OTHER_RUN, flaw)),
        ]);
        assert!(
            require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).is_err(),
            "{flaw}"
        );
    }
    // Control: the same pair without the ambiguous file counts.
    let clean = dir_with(&[
        ("d.txt", answer(DEEPSEEK, RUN, NEW, "ACCEPT")),
        ("g.txt", answer(GLM, OTHER_RUN, NEW, "ACCEPT")),
    ]);
    assert!(require(&gate(&clean, &[HYDROGEN]), &[part(7, NEW, RUN)]).is_ok());
}

#[test]
fn an_ambiguous_answer_claims_its_run_at_every_head() {
    // Its heads conflict, so no part head is safe from it: it blocks the part at OLD as well.
    let dir = dir_with(&[
        ("h.txt", answer(HYDROGEN, RUN, OLD, "ACCEPT")),
        (
            "h-rc.txt",
            ambiguous_rejection(HYDROGEN, RUN, "conflicting-heads"),
        ),
    ]);
    assert!(require(&gate(&dir, &[HYDROGEN]), &[part(7, OLD, RUN)]).is_err());
}

#[test]
fn an_oversized_or_non_text_file_with_the_launcher_header_still_owns_its_run() {
    let accept = answer(HYDROGEN, RUN, NEW, "ACCEPT");
    for kind in ["oversized", "not-utf8"] {
        let dir = dir_with(&[("h.txt", accept.clone())]);
        let mut owned = header(HYDROGEN, RUN).into_bytes();
        owned.push(b'\n');
        if kind == "oversized" {
            owned.extend(std::iter::repeat_n(b'x', (1 << 20) + 16));
        } else {
            owned.extend([0xff, 0xfe, b'\n']);
        }
        std::fs::write(dir.path().join("big.bin"), owned).unwrap();
        let error = require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).unwrap_err();
        assert!(error.contains("more than one answer"), "{kind}: {error}");
    }
}

#[test]
fn subdirectories_are_ignored() {
    let dir = dir_with(&[("h.txt", answer(HYDROGEN, RUN, NEW, "ACCEPT"))]);
    std::fs::create_dir(dir.path().join("nested")).unwrap();
    assert!(require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).is_ok());
}
