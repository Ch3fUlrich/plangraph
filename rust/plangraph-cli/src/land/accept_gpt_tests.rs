use super::accept::{require, AcceptGate, SeatMeta, DEFAULT_SINGLE_SEAT};
use super::accept_tests::{
    answer, dir_with, gate, part, src_root, GPT, HYDROGEN, NEW, OTHER_RUN, RUN,
};

/// The gpt answer; `body_extra` is text the reviewer wrote, placed after the launcher's header line.
fn gpt_answer(body_extra: &str) -> String {
    let text = answer(GPT, RUN, NEW, "ACCEPT");
    let (header, rest) = text.split_once('\n').unwrap();
    format!("{header}\n{body_extra}{rest}")
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
        src_root: Some(src_root()),
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
fn blank_unknown_and_gpt_model_writer_entries_are_refused_by_the_shared_resolver() {
    let dir = dir_with(&[("g.txt", gpt_answer(""))]);
    for families in [
        vec![""],
        vec!["claude", " "],
        vec!["no-such-family-zzz"],
        vec!["gpt-6.2-sol"],
        vec!["claude", "gpt-6.2-sol-high"],
    ] {
        let gate = with_meta(&dir, meta("high", "D-6804", &families));
        assert!(
            require(&gate, &[part(7, NEW, RUN)]).is_err(),
            "{families:?}"
        );
    }
    // A known, different family still counts.
    let gate = with_meta(&dir, meta("high", "D-6804", &["claude"]));
    assert!(require(&gate, &[part(7, NEW, RUN)]).is_ok());
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

/// A planted "checkout" holding a `plangraph.review.family` that answers every name with a planted key.
fn planted_checkout() -> tempfile::TempDir {
    let checkout = tempfile::tempdir().unwrap();
    let package = checkout.path().join("src/plangraph/review");
    std::fs::create_dir_all(&package).unwrap();
    std::fs::write(checkout.path().join("src/plangraph/__init__.py"), "").unwrap();
    std::fs::write(package.join("__init__.py"), "").unwrap();
    std::fs::write(
        package.join("family.py"),
        "class FamilyError(Exception): pass\ndef writer_key(n): return 'planted-' + str(n)\n",
    )
    .unwrap();
    checkout
}

/// The module file the gate's resolver interpreter imports, with the whole environment pointing at `checkout`.
fn imported_family(gate: &AcceptGate, checkout: &std::path::Path) -> String {
    let mut command = gate.python();
    command
        .env("PYTHONPATH", checkout.join("src"))
        .current_dir(checkout);
    let out = command
        .args([
            "-c",
            "import plangraph.review.family as f; print(f.__file__)",
        ])
        .output()
        .unwrap();
    String::from_utf8_lossy(&out.stdout).into_owned()
}

#[test]
fn the_gate_that_land_train_build_constructs_never_takes_its_resolver_from_the_reviewed_checkout() {
    let checkout = planted_checkout();
    let planted = checkout.path().to_str().unwrap();
    let dir = dir_with(&[("g.txt", gpt_answer(""))]);
    // Control: a caller that names the checkout does import the planted resolver.
    let mut trusted = gate(&dir, &[HYDROGEN]);
    trusted.src_root = Some(checkout.path().to_owned());
    assert!(imported_family(&trusted, checkout.path()).contains(planted));
    // The gate built from the build flags, with the planted checkout as the reviewed `--repo`.
    let flags: std::collections::BTreeMap<String, String> = [(
        "accept-dir".to_owned(),
        dir.path().to_str().unwrap().to_owned(),
    )]
    .into();
    let built = super::cli::accept_gate(&flags, checkout.path()).unwrap();
    assert!(built.src_root.is_none());
    assert!(built.python().get_args().any(|arg| arg == "-I"));
    assert!(!imported_family(&built, checkout.path()).contains(planted));
}

#[test]
fn the_build_flags_name_the_accept_dir_the_single_seat_legs_and_the_seat_meta() {
    let dir = tempfile::tempdir().unwrap();
    let meta_path = dir.path().join("meta.json");
    std::fs::write(&meta_path, "{}").unwrap();
    let mut flags = std::collections::BTreeMap::new();
    assert!(
        super::cli::accept_gate(&flags, dir.path()).is_err(),
        "--accept-dir is required"
    );
    flags.insert(
        "accept-dir".to_owned(),
        dir.path().to_str().unwrap().to_owned(),
    );
    let defaults = super::cli::accept_gate(&flags, dir.path()).unwrap();
    assert_eq!(defaults.single_seat.join(","), DEFAULT_SINGLE_SEAT);
    flags.insert(
        "single-seat".to_owned(),
        " Some-Leg , other-leg ,".to_owned(),
    );
    flags.insert(
        "seat-meta".to_owned(),
        meta_path.to_str().unwrap().to_owned(),
    );
    let set = super::cli::accept_gate(&flags, dir.path()).unwrap();
    assert_eq!(set.single_seat, ["some-leg", "other-leg"]);
    std::fs::write(&meta_path, "not json").unwrap();
    assert!(super::cli::accept_gate(&flags, dir.path()).is_err());
}
