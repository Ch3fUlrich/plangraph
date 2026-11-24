use super::build_tests::{part, Fx};
use super::writer_gate::*;

const H1: &str = "1111111111111111111111111111111111111111";
const H2: &str = "2222222222222222222222222222222222222222";

fn check(command: &str, exit_code: i32) -> Check {
    Check {
        command: command.into(),
        exit_code,
    }
}

fn green(pr: u64, head: &str) -> WriterReceipt {
    WriterReceipt {
        pr,
        head: head.into(),
        review_head: head.into(),
        review_verdict: "ACCEPT".into(),
        checks: vec![
            check(
                "python3 tools/affected_tests.py --base origin/main --head HEAD --local --run",
                0,
            ),
            check(
                "python3 tools/preflight.py --base origin/main --head HEAD --lane",
                0,
            ),
        ],
    }
}

#[test]
fn a_part_with_an_accept_and_green_required_checks_on_its_head_is_admitted() {
    let parts = [part(1, H1, None)];
    assert_eq!(refusals(&parts, &[green(1, H1)]), Vec::<String>::new());
    assert!(require(&parts, &[green(1, H1)]).is_ok());
}

#[test]
fn a_part_without_a_receipt_a_review_of_another_head_or_a_red_or_missing_check_is_refused() {
    let parts: Vec<_> = (1..=7).map(|n| part(n, H1, None)).collect();
    let mut other_review = green(2, H1);
    other_review.review_head = H2.into();
    let mut request_changes = green(3, H1);
    request_changes.review_verdict = "REQUEST_CHANGES".into();
    let mut red_tests = green(4, H1);
    red_tests.checks[0].exit_code = 1;
    let mut no_preflight = green(5, H1);
    no_preflight.checks.truncate(1);
    let mut red_extra = green(6, H1);
    red_extra
        .checks
        .push(check("cargo test -p plangraph-cli", 101));
    // A receipt of an earlier head does not cover the head that moved.
    let stale = green(7, H2);
    let found = refusals(
        &parts,
        &[
            other_review,
            request_changes,
            red_tests,
            no_preflight,
            red_extra,
            stale,
        ],
    );
    assert_eq!(found.len(), 7, "{found:#?}");
    assert!(found[0].contains("part #1") && found[0].contains("no writer-host receipt"));
    assert!(found[1].contains("review judged"));
    assert!(found[2].contains("not ACCEPT"));
    assert!(found[3].contains("affected_tests.py") && found[3].contains("exited 1"));
    assert!(found[4].contains("no recorded run of `python3 tools/preflight.py`"));
    assert!(found[5].contains("cargo test") && found[5].contains("101"));
    assert!(found[6].contains("no writer-host receipt"));
    assert!(
        require(&parts, &[green(1, H1)])
            .unwrap_err()
            .lines()
            .count()
            == 6
    );
}

#[test]
fn the_build_command_refuses_without_receipts_before_it_reads_main() {
    let fx = Fx::new();
    let head = fx.commit(&fx.base(), "a.txt", "a\n");
    let dir = tempfile::tempdir().unwrap();
    let parts = dir.path().join("parts.json");
    std::fs::write(
        &parts,
        serde_json::to_vec(&vec![part(1, &head, None)]).unwrap(),
    )
    .unwrap();
    let args: Vec<String> = [
        "build",
        "--repo",
        fx.path().to_str().unwrap(),
        "--base",
        &fx.base(),
        "--name",
        "t1",
        "--parts",
        parts.to_str().unwrap(),
        "--grant-digest",
        "d",
        "--snapshot-position",
        "1",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    let python = || crate::base_fresh::python_command(None);
    let err = super::cli::run_inner(dir.path(), &args, &python).unwrap_err();
    assert!(err.contains("missing --writer-receipts"), "{err}");
}

#[test]
fn the_accepted_receipts_are_written_next_to_the_manifest() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("receipts.json");
    let mine = green(1, H1);
    std::fs::write(
        &file,
        serde_json::to_vec(&vec![green(9, H2), mine.clone()]).unwrap(),
    )
    .unwrap();
    let flags: std::collections::BTreeMap<String, String> = [
        ("writer-receipts", file.to_str().unwrap().to_owned()),
        ("out", dir.path().join("out").to_str().unwrap().to_owned()),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v))
    .collect();
    gate_cli(&flags, &[part(1, H1, None)], "t1").unwrap();
    let written: Vec<WriterReceipt> = serde_json::from_slice(
        &std::fs::read(dir.path().join("out/t1.writer-receipts.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(written, vec![mine]);
}
