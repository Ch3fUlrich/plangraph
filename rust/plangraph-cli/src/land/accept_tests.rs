use super::accept::{parse, require, AcceptGate, SeatMeta, DEFAULT_SINGLE_SEAT};
use super::build_tests::part as built_part;
use super::train::PartIn;

const OLD: &str = "1111111111111111111111111111111111111111";
const NEW: &str = "2222222222222222222222222222222222222222";
const RUN: &str = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_RUN: &str = "run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

const HYDROGEN: &str = "agy-gemini-4-hydrogen-high";
const DEEPSEEK: &str = "opencode-go-deepseek-v4-flash";
const GLM: &str = "opencode-go-glm-5.3-flash";
const GPT: &str = "cx-gpt-6.2-sol-high";

fn answer(leg: &str, run: &str, head: &str, verdict: &str) -> String {
    format!(
        "[served by {leg} via spawner run {run}, status completed]\nVERDICT: {verdict}\n\nFILES READ: src/a.rs\n\nHEAD: {head}\nVERDICT: {verdict}\n"
    )
}

fn part(pr: u64, head: &str, run: &str) -> PartIn {
    let mut part = built_part(pr, head, None);
    part.receipt.run_id = run.into();
    part
}

fn gate(dir: &tempfile::TempDir, single: &[&str]) -> AcceptGate {
    AcceptGate {
        dir: dir.path().to_owned(),
        single_seat: single.iter().map(|leg| (*leg).to_owned()).collect(),
        meta: Default::default(),
    }
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

fn dir_with(files: &[(&str, String)]) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    for (name, text) in files {
        std::fs::write(dir.path().join(name), text).unwrap();
    }
    dir
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
    assert!(error.contains("more than one ACCEPT"), "{error}");
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

fn gpt_answer(body_extra: &str) -> String {
    format!("{body_extra}{}", answer(GPT, RUN, NEW, "ACCEPT"))
}

fn meta(effort: &str, decision: &str, families: &[&str]) -> SeatMeta {
    SeatMeta {
        effort: Some(effort.into()),
        effort_decision: Some(decision.into()),
        writer_families: families.iter().map(|f| (*f).to_owned()).collect(),
    }
}

fn with_meta(dir: &tempfile::TempDir, meta: SeatMeta) -> AcceptGate {
    let mut gate = default_gate(dir);
    gate.meta.insert(RUN.into(), meta);
    gate
}

fn default_gate(dir: &tempfile::TempDir) -> AcceptGate {
    AcceptGate {
        dir: dir.path().to_owned(),
        single_seat: DEFAULT_SINGLE_SEAT.split(',').map(str::to_owned).collect(),
        meta: Default::default(),
    }
}

#[test]
fn the_default_single_seat_set_is_hydrogen_and_gpt() {
    assert_eq!(DEFAULT_SINGLE_SEAT, format!("{HYDROGEN},{GPT}"));
}

#[test]
fn gpt_high_with_the_launcher_metadata_and_another_writer_family_counts_alone() {
    let dir = dir_with(&[("g.txt", gpt_answer(""))]);
    let gate = with_meta(&dir, meta("high", "D-6804", &["claude"]));
    assert!(require(&gate, &[part(7, NEW, RUN)]).is_ok());
}

#[test]
fn gpt_without_the_high_effort_or_the_decision_in_the_metadata_is_refused() {
    let dir = dir_with(&[("g.txt", gpt_answer(""))]);
    for meta in [
        meta("medium", "D-6804", &["claude"]),
        meta("high", "D-1", &["claude"]),
        meta("high", "D-6804", &[]),
        SeatMeta::default(),
    ] {
        let error = require(&with_meta(&dir, meta), &[part(7, NEW, RUN)]).unwrap_err();
        assert!(error.contains("meeting its conditions"), "{error}");
    }
    // No metadata for the run at all.
    assert!(require(&default_gate(&dir), &[part(7, NEW, RUN)]).is_err());
}

#[test]
fn conditions_written_only_in_the_answer_body_never_qualify() {
    // The reviewer's own text claims the card fields; the launcher recorded none for this run.
    let body = "EFFORT: high\nEFFORT_DECISION: D-6804\nWRITER_FAMILIES: claude\n";
    let dir = dir_with(&[("g.txt", gpt_answer(body))]);
    let error = require(&default_gate(&dir), &[part(7, NEW, RUN)]).unwrap_err();
    assert!(error.contains("meeting its conditions"), "{error}");
    // Metadata recorded for another run does not help either.
    let mut other = default_gate(&dir);
    other
        .meta
        .insert(OTHER_RUN.into(), meta("high", "D-6804", &["claude"]));
    assert!(require(&other, &[part(7, NEW, RUN)]).is_err());
}

#[test]
fn a_gpt_review_of_a_gpt_written_head_is_refused() {
    let dir = dir_with(&[("g.txt", gpt_answer(""))]);
    for family in ["gpt", "openai", "GPT", "codex"] {
        let gate = with_meta(&dir, meta("high", "D-6804", &["claude", family]));
        assert!(require(&gate, &[part(7, NEW, RUN)]).is_err(), "{family}");
    }
}

#[test]
fn hydrogen_needs_no_card_conditions_and_the_override_replaces_the_default() {
    let dir = dir_with(&[("h.txt", answer(HYDROGEN, RUN, NEW, "ACCEPT"))]);
    assert!(require(&default_gate(&dir), &[part(7, NEW, RUN)]).is_ok());
    let gpt = dir_with(&[("g.txt", gpt_answer(""))]);
    let mut only_hydrogen = gate(&gpt, &[HYDROGEN]);
    only_hydrogen
        .meta
        .insert(RUN.into(), meta("high", "D-6804", &["claude"]));
    assert!(require(&only_hydrogen, &[part(7, NEW, RUN)]).is_err());
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
fn only_a_full_head_and_a_run_header_parse() {
    let text = |head: &str| answer(HYDROGEN, RUN, head, "ACCEPT");
    assert!(parse(&text("2222222")).is_none());
    let accept = parse(&text(NEW)).unwrap();
    assert_eq!((accept.head.as_str(), accept.run_id.as_str()), (NEW, RUN));
    assert_eq!(accept.leg, HYDROGEN);
}

/// The check sits in `build_train` itself, so no caller can publish a train without it.
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

#[test]
fn the_seat_meta_file_is_read_by_run_id_and_a_bad_file_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("meta.json");
    std::fs::write(
        &path,
        format!(
            r#"{{"{RUN}": {{"effort": "high", "effort_decision": "D-6804", "writer_families": ["claude"]}}}}"#
        ),
    )
    .unwrap();
    let read = AcceptGate::read_meta(&path).unwrap();
    assert_eq!(read[RUN].effort.as_deref(), Some("high"));
    std::fs::write(&path, "not json").unwrap();
    assert!(AcceptGate::read_meta(&path).is_err());
    assert!(AcceptGate::read_meta(&dir.path().join("missing.json")).is_err());
}
