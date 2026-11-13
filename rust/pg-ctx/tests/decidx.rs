use pg_ctx::decidx::{parse, write_tsv};

const LOG: &str = "# Synthetic decisions\n\
not a row: D-99\n\
| id | project | decision | by | why / evidence |\n\
|---|---|---|---|---|\n\
| D-10 | demo | Withdrawn pending clarification. | mechanical | fixture |\n\
| D-9 | demo | Supersedes D-4; lifts D-3; withdraws D-2. | router | fixture |\n\
| D-8 | demo | Stable policy; by operator. | operator, router | fixture |\n\
| D-7 | demo | Notify the operator after a mechanical update. | mechanical | fixture |\n\
| D-6 | demo | One-off choice; by operator. | operator | fixture |\n\
| D-4 | demo | Prior route. | routing-00 | fixture |\n\
| D-3 | demo | Prior hold. | mechanical | fixture |\n\
| D-2 | demo | Prior choice; by operator. | operator | fixture |\n";
const RULES: &[(&str, &str)] = &[("9.14", "Stable policy (D-8); old choice (D-2).")];

#[test]
fn eight_rows_cover_status_kind_and_edges() {
    let rows = parse(LOG, RULES, "by operator").unwrap();
    let actual: Vec<_> = rows
        .iter()
        .map(|r| (r.id.as_str(), r.kind.as_str(), r.status.as_str()))
        .collect();
    assert_eq!(
        actual,
        vec![
            ("D-2", "operator", "superseded_by D-9"),
            ("D-3", "mechanical", "superseded_by D-9"),
            ("D-4", "router", "superseded_by D-9"),
            ("D-6", "operator", "history"),
            ("D-7", "mechanical", "history"),
            ("D-8", "operator", "in_force 9.14"),
            ("D-9", "router", "history"),
            ("D-10", "mechanical", "unknown"),
        ]
    );
    assert!(rows.iter().all(|r| r.text_hash.len() == 64
        && r.text_hash
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())));
}

#[test]
fn duplicate_id_is_an_error() {
    assert_eq!(
        parse(&format!("{LOG}{LOG}"), RULES, "by operator").unwrap_err(),
        "duplicate D-id: D-10"
    );
}

#[test]
fn two_runs_are_byte_identical_even_with_reversed_input() {
    let dir = tempfile::tempdir().unwrap();
    let reversed = LOG.lines().rev().collect::<Vec<_>>().join("\n");
    for (name, input) in [("first.tsv", LOG), ("second.tsv", reversed.as_str())] {
        let mut rows = parse(input, RULES, "by operator").unwrap();
        rows.reverse(); // The writer must sort, not rely on the parser's order.
        write_tsv(&rows, std::fs::File::create(dir.path().join(name)).unwrap()).unwrap();
    }
    let first = std::fs::read(dir.path().join("first.tsv")).unwrap();
    assert_eq!(first, std::fs::read(dir.path().join("second.tsv")).unwrap());
    let text = String::from_utf8(first).unwrap();
    assert!(text.starts_with("d_id\tkind\tstatus\ttext_hash\nD-2\toperator\tsuperseded_by D-9\t"));
    assert_eq!(text.lines().count(), 9);
}

#[test]
fn only_changed_row_hash_changes_and_whitespace_is_normalized() {
    let before = parse(LOG, RULES, "by operator").unwrap();
    let mut after = parse(
        &LOG.replace("mechanical update", "mechanical revision"),
        RULES,
        "by operator",
    )
    .unwrap();
    let mut old_tsv = Vec::new();
    let mut new_tsv = Vec::new();
    write_tsv(&before, &mut old_tsv).unwrap();
    write_tsv(&after, &mut new_tsv).unwrap();
    let changed: Vec<_> = old_tsv
        .split(|b| *b == b'\n')
        .zip(new_tsv.split(|b| *b == b'\n'))
        .filter(|(old, new)| old != new)
        .collect();
    assert_eq!(changed.len(), 1);
    assert!(changed[0].0.starts_with(b"D-7\t"));
    assert_eq!(
        changed[0]
            .0
            .split(|b| *b == b'\t')
            .take(3)
            .collect::<Vec<_>>(),
        changed[0]
            .1
            .split(|b| *b == b'\t')
            .take(3)
            .collect::<Vec<_>>()
    );
    for (old, new) in before.iter().zip(after.iter_mut()) {
        if old.id == "D-7" {
            assert_ne!(old.text_hash, new.text_hash);
            new.text_hash.clone_from(&old.text_hash);
            new.text.clone_from(&old.text);
        }
        assert_eq!(old, new);
    }
    assert_eq!(
        before,
        parse(&LOG.replace("demo |", " demo\t |"), RULES, "by operator").unwrap()
    );
    assert_eq!(
        parse("| D-1 | abc |", &[], "").unwrap()[0].text_hash,
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
}

#[test]
fn passive_edges_rule_boundaries_and_malformed_rows() {
    let log = "| D-1 | demo | Superseded by D-5. | router | fixture |\n\
               | D-2 | demo | D-2 GO WITHDRAWN by D-5. | router | fixture |\n\
               | D-3 | demo | D-3 HOLD LIFTED. | router | fixture |\n\
               | D-4 | incomplete |\n";
    let rows = parse(log, &[("1.0", "D-10, D-20, D-30")], "").unwrap();
    assert_eq!(
        rows.iter().map(|r| r.status.as_str()).collect::<Vec<_>>(),
        [
            "superseded_by D-5",
            "superseded_by D-5",
            "unknown",
            "unknown"
        ]
    );
    assert!(rows.iter().all(|r| r.kind != "operator"));
}

#[test]
fn escaped_pipes_marker_parameter_and_exact_rule_references() {
    let log = "| D-1 | demo | Literal \\| pipe; AUTHORED BY HUMAN. | router | fixture |";
    let rows = parse(log, &[("2.0", "D-10 only")], "authored by human").unwrap();
    assert_eq!(
        (rows[0].kind.as_str(), rows[0].status.as_str()),
        ("operator", "history")
    );
    assert!(rows[0].text.contains("Literal \\| pipe"));
    assert_eq!(parse(log, &[], "by operator").unwrap()[0].kind, "router");
    assert_eq!(
        parse(log, &[("2.0", "D-1"), ("1.0", "D-1")], "").unwrap()[0].status,
        "in_force 1.0,2.0"
    );
}

#[test]
fn conflicting_supersession_is_unknown_and_io_errors_propagate() {
    let log = "| D-1 | demo | Prior choice. | router | fixture |\n\
               | D-2 | demo | D-1 GO WITHDRAWN. | router | fixture |\n\
               | D-3 | demo | Supersedes D-1. | router | fixture |";
    let rows = parse(log, &[], "").unwrap();
    assert_eq!(rows[0].status, "unknown");
    assert!(write_tsv(&rows, &mut [0_u8; 1][..]).is_err());
}

#[test]
fn past_tense_active_wording_names_the_replaced_decision() {
    for wording in ["Withdrew", "Superseded", "Lifted"] {
        let log = format!("| D-1 | demo | Prior choice. | router | fixture |\n| D-2 | demo | {wording} D-1. | router | fixture |");
        let rows = parse(&log, &[], "").unwrap();
        assert_eq!(rows[0].status, "superseded_by D-2", "{wording}");
    }
}
