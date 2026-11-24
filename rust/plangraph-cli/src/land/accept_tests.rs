use super::accept::{require, AcceptGate};
use super::build_tests::part as built_part;
use super::train::PartIn;

pub(super) const OLD: &str = "1111111111111111111111111111111111111111";
pub(super) const NEW: &str = "2222222222222222222222222222222222222222";
pub(super) const RUN: &str = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
pub(super) const OTHER_RUN: &str = "run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

pub(super) const HYDROGEN: &str = "agy-gemini-4-hydrogen-high";
pub(super) const DEEPSEEK: &str = "opencode-go-deepseek-v4-flash";
pub(super) const GLM: &str = "opencode-go-glm-5.3-flash";
pub(super) const GPT: &str = "cx-gpt-6.2-sol-high";

pub(super) fn answer(leg: &str, run: &str, head: &str, verdict: &str) -> String {
    format!(
        "[served by {leg} via spawner run {run}, status completed]\nVERDICT: {verdict}\n\nFILES READ: src/a.rs\n\nHEAD: {head}\nVERDICT: {verdict}\n"
    )
}

pub(super) fn part(pr: u64, head: &str, run: &str) -> PartIn {
    let mut part = built_part(pr, head, None);
    part.receipt.run_id = run.into();
    part
}

/// The checkout holding `src/plangraph`, for the Python writer-family resolver.
pub(super) fn src_root() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

pub(super) fn gate(dir: &tempfile::TempDir, single: &[&str]) -> AcceptGate {
    AcceptGate {
        dir: dir.path().to_owned(),
        single_seat: single.iter().map(|leg| (*leg).to_owned()).collect(),
        meta: Default::default(),
        src_root: Some(src_root()),
    }
}

pub(super) fn dir_with(files: &[(&str, String)]) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    for (name, text) in files {
        std::fs::write(dir.path().join(name), text).unwrap();
    }
    dir
}

/// One counted answer per part (the exact single-seat leg, bound to the receipt run), for build tests.
pub(crate) fn counted_for(parts: &[PartIn]) -> (tempfile::TempDir, AcceptGate) {
    let dir = tempfile::tempdir().unwrap();
    for part in parts {
        let text = answer(HYDROGEN, &part.receipt.run_id, &part.head, "ACCEPT");
        std::fs::write(dir.path().join(format!("p{}.txt", part.pr)), text).unwrap();
    }
    let gate = gate(&dir, &[HYDROGEN]);
    (dir, gate)
}

#[test]
fn the_exact_single_seat_leg_bound_to_the_receipt_run_counts() {
    let dir = dir_with(&[("h.txt", answer(HYDROGEN, RUN, NEW, "ACCEPT"))]);
    let lines = require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).unwrap();
    assert_eq!(
        lines,
        [format!("accept #7 head {NEW} run {RUN} file h.txt")]
    );
}

#[test]
fn the_required_pair_counts_on_two_runs_and_one_of_them_alone_does_not() {
    let deepseek = answer(DEEPSEEK, RUN, NEW, "ACCEPT");
    let glm = answer(GLM, OTHER_RUN, NEW, "ACCEPT");
    let both = dir_with(&[("d.txt", deepseek.clone()), ("g.txt", glm.clone())]);
    for run in [RUN, OTHER_RUN] {
        assert!(require(&gate(&both, &[HYDROGEN]), &[part(7, NEW, run)]).is_ok());
    }
    for (name, text, run) in [("d.txt", deepseek, RUN), ("g.txt", glm, OTHER_RUN)] {
        let one = dir_with(&[(name, text)]);
        let error = require(&gate(&one, &[HYDROGEN]), &[part(7, NEW, run)]).unwrap_err();
        assert!(
            error.contains("neither a single-seat leg meeting its conditions nor both"),
            "{error}"
        );
    }
}

#[test]
fn the_pair_on_one_run_or_with_a_duplicate_run_never_counts() {
    // Two files naming the same run satisfy no pair: the runs must differ.
    let same = dir_with(&[
        ("d.txt", answer(DEEPSEEK, RUN, NEW, "ACCEPT")),
        ("g.txt", answer(GLM, RUN, NEW, "ACCEPT")),
    ]);
    assert!(require(&gate(&same, &[HYDROGEN]), &[part(7, NEW, RUN)]).is_err());
    // A run id repeated in two answers at the head is ambiguous, whatever the legs.
    let twice = dir_with(&[
        ("h1.txt", answer(HYDROGEN, RUN, NEW, "ACCEPT")),
        ("h2.txt", answer(HYDROGEN, RUN, NEW, "ACCEPT")),
    ]);
    let error = require(&gate(&twice, &[HYDROGEN]), &[part(7, NEW, RUN)]).unwrap_err();
    assert!(error.contains("more than one answer"), "{error}");
}

#[test]
fn a_second_pair_run_claimed_by_another_answer_never_counts() {
    // The receipt run is the deepseek answer; the glm run is also claimed by an unrelated leg's answer.
    let dir = dir_with(&[
        ("d.txt", answer(DEEPSEEK, RUN, NEW, "ACCEPT")),
        ("g.txt", answer(GLM, OTHER_RUN, NEW, "ACCEPT")),
        ("x.txt", answer("some-other-leg", OTHER_RUN, NEW, "ACCEPT")),
    ]);
    assert!(require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).is_err());
    // Without the duplicate the same two answers count.
    let clean = dir_with(&[
        ("d.txt", answer(DEEPSEEK, RUN, NEW, "ACCEPT")),
        ("g.txt", answer(GLM, OTHER_RUN, NEW, "ACCEPT")),
    ]);
    assert!(require(&gate(&clean, &[HYDROGEN]), &[part(7, NEW, RUN)]).is_ok());
}

#[test]
fn leg_ids_are_compared_whole_never_as_substrings() {
    // A leg that merely contains an approved id, or a made-up leg naming deepseek and glm, is not one.
    let lookalikes = dir_with(&[
        (
            "a.txt",
            answer("x-opencode-go-deepseek-v4-flash-y", RUN, NEW, "ACCEPT"),
        ),
        ("b.txt", answer("glm", OTHER_RUN, NEW, "ACCEPT")),
        ("c.txt", answer("hydrogen", "run-ccc", NEW, "ACCEPT")),
    ]);
    for run in [RUN, OTHER_RUN, "run-ccc"] {
        assert!(require(&gate(&lookalikes, &[HYDROGEN]), &[part(7, NEW, run)]).is_err());
    }
}

#[test]
fn an_accept_for_another_run_never_binds_the_part() {
    let dir = dir_with(&[
        ("h.txt", answer(HYDROGEN, OTHER_RUN, NEW, "ACCEPT")),
        ("x.txt", answer(GLM, OTHER_RUN, NEW, "ACCEPT")),
    ]);
    let error = require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).unwrap_err();
    assert!(
        error.contains("no ACCEPT at this head carries the receipt run"),
        "{error}"
    );
    assert!(error.contains(RUN) && error.contains("part #7"), "{error}");
}

#[test]
fn a_stale_head_is_refused_and_named() {
    let dir = dir_with(&[("p1-r1.txt", answer(HYDROGEN, RUN, OLD, "ACCEPT"))]);
    let error = require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).unwrap_err();
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
        ("rc.txt", answer(HYDROGEN, RUN, NEW, "REQUEST_CHANGES")),
        ("unserved.txt", unserved),
        ("p.status", status),
        (
            "failed.txt",
            answer(HYDROGEN, RUN, NEW, "ACCEPT").replace("completed", "failed"),
        ),
    ]);
    assert!(require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).is_err());
}

#[test]
fn a_completed_request_changes_on_a_pair_run_conflicts_with_its_accept() {
    // The glm run has an ACCEPT and a REQUEST_CHANGES at the same head: it owns two answers.
    let dir = dir_with(&[
        ("d.txt", answer(DEEPSEEK, RUN, NEW, "ACCEPT")),
        ("g.txt", answer(GLM, OTHER_RUN, NEW, "ACCEPT")),
        ("g2.txt", answer(GLM, OTHER_RUN, NEW, "REQUEST_CHANGES")),
    ]);
    assert!(require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).is_err());
    // The same for the receipt run itself, and the receipt answer must accept.
    let receipt = dir_with(&[
        ("d.txt", answer(DEEPSEEK, RUN, NEW, "ACCEPT")),
        ("d2.txt", answer(DEEPSEEK, RUN, NEW, "REQUEST_CHANGES")),
        ("g.txt", answer(GLM, OTHER_RUN, NEW, "ACCEPT")),
    ]);
    assert!(require(&gate(&receipt, &[HYDROGEN]), &[part(7, NEW, RUN)]).is_err());
    let rejected = dir_with(&[("h.txt", answer(HYDROGEN, RUN, NEW, "REQUEST_CHANGES"))]);
    let error = require(&gate(&rejected, &[HYDROGEN]), &[part(7, NEW, RUN)]).unwrap_err();
    assert!(error.contains("does not accept"), "{error}");
}

#[test]
fn every_part_needs_its_own_evidence_and_the_single_seat_set_is_configurable() {
    let dir = dir_with(&[("h.txt", answer("some-other-leg", RUN, NEW, "ACCEPT"))]);
    assert!(require(&gate(&dir, &[HYDROGEN]), &[part(7, NEW, RUN)]).is_err());
    let other = gate(&dir, &["some-other-leg"]);
    assert!(require(&other, &[part(7, NEW, RUN)]).is_ok());
    assert!(require(&other, &[part(7, NEW, RUN), part(8, OLD, OTHER_RUN)]).is_err());
}

#[test]
fn build_train_refuses_a_stale_head_and_builds_an_exact_head() {
    use super::build_tests::Fx;
    use super::cli::{build_train, Build};
    use super::git::POLICY_VERSION;
    use super::main_state::State;
    use super::train::BuildOpen;
    let fx = Fx::new();
    let base = fx.base();
    let head = fx.commit(&base, "a.txt", "a\n");
    let out = tempfile::tempdir().unwrap();
    let parts = vec![built_part(1, &head, None)];
    let (_dir, exact) = counted_for(&parts);
    let stale_dir = dir_with(&[("old.txt", answer(HYDROGEN, "run-1", OLD, "ACCEPT"))]);
    let job = |accept: AcceptGate, name: &str| Build {
        repo: fx.path().to_owned(),
        base: base.clone(),
        name: name.into(),
        parts: parts.clone(),
        attributes: String::new(),
        cap: None,
        out: out.path().to_owned(),
        branch: format!("land/build/{name}"),
        resolutions: vec![],
        protected: vec![],
        accept,
        open: BuildOpen {
            build: name.into(),
            policy_version: POLICY_VERSION.into(),
            base: base.clone(),
            grant_digest: "g".into(),
            snapshot_position: 1,
        },
    };
    let error = build_train(
        &job(gate(&stale_dir, &[HYDROGEN]), "stale"),
        &State::Green,
        &base,
    )
    .unwrap_err();
    assert!(error.contains("accept: part #1"), "{error}");
    assert!(
        !out.path().join("stale.open.json").exists(),
        "nothing is written"
    );
    assert!(!out.path().join("stale.manifest.json").exists());
    let lines = build_train(&job(exact, "exact"), &State::Green, &base).unwrap();
    assert!(
        lines.iter().any(|l| l.starts_with("accept #1 head ")),
        "{lines:?}"
    );
    assert!(out.path().join("exact.manifest.json").exists());
}
