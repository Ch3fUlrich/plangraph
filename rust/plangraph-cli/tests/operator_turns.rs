//! Public-API tests of the operator-turns reader against the shared fixtures.
use plangraph_cli::operator_turns::{
    citation, cited_refs, format_row, live_lifts, load, parse, validate, Entry, Kind, Lift, PinRef,
    PinnedTurn, Problem, HEADER, TSV_PATH,
};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const H1: &str = "a7529611e6c74d1d4e1356b338de9c50ca6708c68daac64715c584bc3b08c384";
const H2: &str = "0523017060d5ca57c158bcefa357197b257499c7234756cfde382939c12028f7";
const H3: &str = "4cd0e21a9a0795a14ec9aa5f0e7d1abff0492565770e43eafdf1e3e8afed1f33";
const H4: &str = "085a3980ac87b5877d8781dc9ca1819642602272ad135c0e812545fce8810d3f";

/// The checked-in fixtures, materialized once per process with CRLF restored to LF: a checkout
/// with `core.autocrlf` converts the hashed bytes, and no fixture holds a CR.
fn fixture(name: &str) -> PathBuf {
    static ROOT: Mutex<Option<tempfile::TempDir>> = Mutex::new(None);
    let mut root = ROOT.lock().unwrap();
    let base = root
        .get_or_insert_with(|| tempfile::tempdir().unwrap())
        .path()
        .to_path_buf();
    let target = base.join(name);
    if !target.exists() {
        let source = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/operator_turns")
            .join(name);
        copy_tree(&source, &target);
    }
    target
}

/// Copies a tree, undoing an end-of-line conversion of the checkout.
fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            let bytes = fs::read(entry.path()).unwrap();
            let mut restored = Vec::with_capacity(bytes.len());
            for (i, b) in bytes.iter().enumerate() {
                if !(*b == b'\r' && bytes.get(i + 1) == Some(&b'\n')) {
                    restored.push(*b);
                }
            }
            fs::write(target, restored).unwrap();
        }
    }
}

fn copy_of(name: &str) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    copy_tree(&fixture(name), dir.path());
    dir
}

fn tsv_text(dir: &Path) -> String {
    fs::read_to_string(dir.join(TSV_PATH)).unwrap()
}

fn append_row(dir: &Path, row: &[&str]) {
    let mut text = tsv_text(dir);
    text.push_str(&row.join("\t"));
    text.push('\n');
    fs::write(dir.join(TSV_PATH), text).unwrap();
}

fn codes(problems: &[Problem]) -> Vec<(usize, &'static str)> {
    problems.iter().map(|p| (p.line, p.code)).collect()
}

fn valid_rows() -> Vec<PinnedTurn> {
    load(&fixture("valid")).unwrap()
}

fn lift(item: &str) -> Lift {
    Lift {
        item: item.into(),
        gate: "operator".into(),
    }
}

fn items(turns: &[PinnedTurn], decision: &str) -> Vec<String> {
    live_lifts(turns, decision)
        .into_iter()
        .map(|l| l.item.clone())
        .collect()
}

#[test]
fn valid_fixture_loads() {
    let rows = valid_rows();
    assert_eq!(rows.len(), 7);
    let l2 = &rows[0];
    assert_eq!(l2.line, 2);
    assert_eq!(l2.kind, Kind::Human);
    assert_eq!(l2.decision, "D-101");
    assert_eq!(l2.lifts.len(), 2);
    assert_eq!(rows[1].kind, Kind::Relay);
    assert_eq!(
        rows[1].relay_of,
        [PinRef {
            turn_sha256: H1.into(),
            utc_timestamp: "2026-01-05T10:00:00.000Z".into(),
            decision: "D-101".into()
        }]
    );
    assert_eq!(rows[2].withdrawn_by.as_deref(), Some("D-105"));
    assert_eq!(rows[5].decision, "D-107");
    assert_eq!(rows[6].decision, "D-108");
    assert_eq!(rows[0].turn_ref(), format!("{H1}@2026-01-05T10:00:00.000Z"));
}

#[test]
fn live_lifts_follow_withdrawal_and_relay_source() {
    let rows = valid_rows();
    assert_eq!(items(&rows, "D-101"), ["demo:A", "demo:X;Y(1):z"]);
    assert_eq!(items(&rows, "D-102"), ["demo:A"]);
    for none in ["D-103", "D-104", "D-106", "D-999"] {
        assert!(items(&rows, none).is_empty(), "{none}");
    }
    assert_eq!(items(&rows, "D-107"), ["demo:C"]);
    assert_eq!(items(&rows, "D-108"), ["demo:C"]);
}

#[test]
fn relay_source_survives_backfill_and_sibling_pins() {
    let dir = copy_of("valid");
    let u1 = "00000000-0000-4000-8000-000000000001";
    let b = r#"[{"item":"demo:B","gate":"operator"}]"#;
    // A live pin of the same text between the withdrawn source and its relay.
    append_row(
        dir.path(),
        &[
            "1",
            H3,
            "2026-01-05T11:02:00.000Z",
            "human",
            "-",
            u1,
            "31",
            "D-109",
            b,
            "-",
        ],
    );
    // A live sibling pin on the exact source turn of the relay.
    append_row(
        dir.path(),
        &[
            "1",
            H3,
            "2026-01-05T11:00:00.000Z",
            "human",
            "-",
            u1,
            "30",
            "D-110",
            b,
            "-",
        ],
    );
    let rows = load(dir.path()).unwrap();
    assert_eq!(items(&rows, "D-109"), ["demo:B"]);
    assert_eq!(items(&rows, "D-110"), ["demo:B"]);
    assert!(items(&rows, "D-104").is_empty());
    assert!(items(&rows, "D-103").is_empty());
}

#[test]
fn relay_scope_mismatch_is_refused() {
    let err = load(&fixture("relay-scope-mismatch")).unwrap_err();
    assert_eq!(codes(&err), [(3, "relay_scope")]);
    assert!(err[0].detail.contains("demo:C"));
}

#[test]
fn missing_text_file_is_refused() {
    let dir = copy_of("valid");
    fs::remove_file(dir.path().join(format!("router/operator-turns/{H2}.txt"))).unwrap();
    assert_eq!(codes(&load(dir.path()).unwrap_err()), [(3, "text_missing")]);
}

#[test]
fn text_hash_mismatch_is_refused() {
    let dir = copy_of("valid");
    let path = dir.path().join(format!("router/operator-turns/{H3}.txt"));
    let mut bytes = fs::read(&path).unwrap();
    bytes.push(b'!');
    fs::write(&path, bytes).unwrap();
    let err = load(dir.path()).unwrap_err();
    assert_eq!(codes(&err), [(4, "text_hash"), (7, "text_hash")]);
    assert!(err.iter().all(|p| !p.detail.contains("go!")));
}

#[test]
fn keys_with_semicolon_paren_colon_round_trip() {
    let text = tsv_text(&fixture("valid"));
    let rows = valid_rows();
    for (row, line) in rows.iter().zip(text.lines().skip(1)) {
        assert_eq!(format_row(row), line);
    }
    let mut turn = rows[0].clone();
    turn.lifts = vec![lift(r#"demo:Q"x\y;(z):1"#)];
    let row = format_row(&turn);
    assert_eq!(row.matches('\t').count(), 9);
    let parsed = parse(&format!("{HEADER}\n{row}\n")).unwrap();
    assert_eq!(parsed[0].lifts, turn.lifts);
    assert_eq!(format_row(&parsed[0]), row);
}

/// The first data line of the valid fixture as fields, one column replaced.
fn mutated(line: usize, column: usize, value: &str) -> String {
    let text = tsv_text(&fixture("valid"));
    let mut fields: Vec<String> = text
        .lines()
        .nth(line - 1)
        .unwrap()
        .split('\t')
        .map(String::from)
        .collect();
    fields[column] = value.into();
    format!("{HEADER}\n{}\n", fields.join("\t"))
}

#[test]
fn malformed_rows_name_their_column() {
    let upper = H1.to_uppercase();
    let hash_h2 = format!("{H2}@2026-01-05T10:00:00.000Z@D-101");
    let cases: Vec<(String, &str)> = vec![
        (mutated(2, 0, "2"), "version"),
        (mutated(2, 1, &upper), "field:turn_sha256"),
        (mutated(2, 2, "2026-01-05T10:00:00Z"), "field:utc_timestamp"),
        (
            mutated(2, 2, "2026-01-05T10:00:00.000+00:00"),
            "field:utc_timestamp",
        ),
        (
            mutated(2, 2, "2026-02-30T10:00:00.000Z"),
            "field:utc_timestamp",
        ),
        (mutated(2, 3, "operator"), "field:kind"),
        (mutated(2, 4, &hash_h2), "field:relay_of"),
        (mutated(3, 4, "-"), "field:relay_of"),
        (
            mutated(3, 4, &format!("{H1}@2026-01-05T10:00:00.000Z")),
            "field:relay_of",
        ),
        (
            mutated(2, 5, "00000000-0000-4000-8000-00000000000G"),
            "field:session_uuid",
        ),
        (mutated(2, 6, "0"), "field:transcript_line"),
        (mutated(2, 6, "012"), "field:transcript_line"),
        (mutated(2, 7, "D-0101"), "field:decision"),
        (mutated(2, 7, "D-2,D-1"), "field:decision"),
        (mutated(2, 7, "D-1, D-2"), "field:decision"),
        (
            mutated(2, 8, r#"[{"item":"demo:A", "gate":"operator"}]"#),
            "field:lifts",
        ),
        (
            mutated(2, 8, r#"[{"gate":"operator","item":"demo:A"}]"#),
            "field:lifts",
        ),
        (
            mutated(2, 8, r#"[{"item":"demo:A","gate":"operator","x":1}]"#),
            "field:lifts",
        ),
        (
            mutated(
                2,
                8,
                r#"[{"item":"demo:A","gate":"operator"},{"item":"demo:A","gate":"operator"}]"#,
            ),
            "field:lifts",
        ),
        (
            mutated(2, 8, r#"[{"item":"demoA","gate":"operator"}]"#),
            "field:lifts",
        ),
        (
            mutated(2, 8, r#"[{"item":"demo:A","gate":"Operator"}]"#),
            "field:lifts",
        ),
        (mutated(2, 9, "D-100"), "field:withdrawn_by"),
        (mutated(2, 9, "D-101"), "field:withdrawn_by"),
        (mutated(2, 4, ""), "field:relay_of"),
        (mutated(2, 4, " -"), "field:relay_of"),
    ];
    for (text, code) in cases {
        let err = parse(&text).unwrap_err();
        assert_eq!(err.len(), 1, "{text}: {err:?}");
        assert_eq!((err[0].line, err[0].code), (2, code), "{text}");
        assert!(err[0]
            .to_string()
            .starts_with("router/operator-turns.tsv:2: "));
    }
}

#[test]
fn malformed_files_are_refused() {
    let good = tsv_text(&fixture("valid"));
    let wrong_header = good.replacen("decision", "decisions", 1);
    assert_eq!(codes(&parse(&wrong_header).unwrap_err()), [(1, "header")]);
    let nine = format!("{HEADER}\n{}\n", ["1"; 9].join("\t"));
    assert_eq!(codes(&parse(&nine).unwrap_err()), [(2, "columns")]);
    let eleven = format!("{HEADER}\n{}\n", ["1"; 11].join("\t"));
    assert_eq!(codes(&parse(&eleven).unwrap_err()), [(2, "columns")]);
    let blank = format!("{HEADER}\n\n");
    assert_eq!(codes(&parse(&blank).unwrap_err()), [(2, "columns")]);
    assert_eq!(
        codes(&parse(&good.replace('\n', "\r\n")).unwrap_err()),
        [(0, "line_ending")]
    );
    assert_eq!(
        codes(&parse(good.trim_end()).unwrap_err()),
        [(0, "line_ending")]
    );
    assert_eq!(codes(&parse("").unwrap_err()), [(0, "header")]);
    // Two faulty rows give two problems, in line order.
    let two = format!(
        "{}{}",
        mutated(2, 3, "x"),
        mutated(3, 0, "9").lines().nth(1).unwrap()
    );
    let err = parse(&format!("{two}\n")).unwrap_err();
    assert_eq!(codes(&err), [(2, "field:kind"), (3, "version")]);
    // A BOM and a non-UTF-8 table are refused by the shared validator.
    let tsv = |bytes: Vec<u8>| Entry {
        name: "t".into(),
        regular: true,
        bytes,
    };
    let mut bom = vec![0xef, 0xbb, 0xbf];
    bom.extend(good.as_bytes());
    assert_eq!(
        codes(&validate(&tsv(bom), &[]).unwrap_err()),
        [(0, "encoding")]
    );
    assert_eq!(
        codes(&validate(&tsv(vec![0xff, 0xfe]), &[]).unwrap_err()),
        [(0, "encoding")]
    );
}

fn rows_of(lines: &[Vec<String>]) -> String {
    let mut out = format!("{HEADER}\n");
    for l in lines {
        out.push_str(&l.join("\t"));
        out.push('\n');
    }
    out
}

fn fields(line: usize) -> Vec<String> {
    tsv_text(&fixture("valid"))
        .lines()
        .nth(line - 1)
        .unwrap()
        .split('\t')
        .map(String::from)
        .collect()
}

#[test]
fn cross_row_rules() {
    // The same turn naming D-101 twice.
    let err = parse(&rows_of(&[fields(2), fields(2)])).unwrap_err();
    assert_eq!(codes(&err), [(3, "duplicate_pin")]);
    // The same turn with another transcript line.
    let mut moved = fields(2);
    moved[6] = "13".into();
    moved[7] = "D-120".into();
    let err = parse(&rows_of(&[fields(2), moved])).unwrap_err();
    assert_eq!(codes(&err), [(3, "turn_conflict")]);
    // Withdrawal is decision-wide: every pin of a D-id carries the same withdrawn_by.
    let mut second_turn = fields(4);
    second_turn[2] = "2026-01-05T11:30:00.000Z".into();
    second_turn[9] = "-".into();
    let err = parse(&rows_of(&[fields(4), second_turn])).unwrap_err();
    assert_eq!(codes(&err), [(3, "withdrawal_conflict")]);
    // An unknown relay_of, a source of another decision, and a source later than the relay.
    let mut unknown = fields(3);
    unknown[4] = format!("{H4}@2026-01-05T10:00:00.000Z@D-101");
    assert_eq!(
        codes(&parse(&rows_of(&[fields(2), unknown])).unwrap_err()),
        [(3, "relay_source_missing")]
    );
    let mut wrong_decision = fields(3);
    wrong_decision[4] = format!("{H1}@2026-01-05T10:00:00.000Z@D-106");
    assert_eq!(
        codes(&parse(&rows_of(&[fields(2), wrong_decision])).unwrap_err()),
        [(3, "relay_source_missing")]
    );
    let mut early = fields(3);
    early[2] = "2026-01-05T09:00:00.000Z".into();
    assert_eq!(
        codes(&parse(&rows_of(&[fields(2), early])).unwrap_err()),
        [(3, "relay_source_missing")]
    );
    // A relay may not cite a relay.
    let mut chained = fields(5);
    chained[4] = format!("{H2}@2026-01-05T10:05:00.000Z@D-102");
    let err = parse(&rows_of(&[fields(2), fields(3), chained])).unwrap_err();
    assert_eq!(codes(&err), [(4, "relay_source_missing")]);
}

#[test]
fn text_directory_rules() {
    let dir = copy_of("valid");
    let texts = dir.path().join("router/operator-turns");
    fs::write(texts.join("stray.txt"), "x").unwrap();
    let err = load(dir.path()).unwrap_err();
    assert_eq!(codes(&err), [(0, "orphan_text")]);
    assert!(!err[0].detail.contains("stray"));
    fs::remove_file(texts.join("stray.txt")).unwrap();
    fs::write(texts.join(format!("{}.txt", "f".repeat(64))), "x").unwrap();
    assert_eq!(codes(&load(dir.path()).unwrap_err()), [(0, "orphan_text")]);
    fs::remove_file(texts.join(format!("{}.txt", "f".repeat(64)))).unwrap();

    fs::write(texts.join(format!("{H4}.txt")), "").unwrap();
    assert_eq!(
        codes(&load(dir.path()).unwrap_err()),
        [(5, "text_not_regular")]
    );

    #[cfg(unix)]
    {
        use std::os::unix::fs::symlink;
        let copy = copy_of("valid");
        let path = copy.path().join(format!("router/operator-turns/{H2}.txt"));
        let real = copy.path().join("elsewhere.txt");
        fs::rename(&path, &real).unwrap();
        symlink(&real, &path).unwrap();
        assert_eq!(
            codes(&load(copy.path()).unwrap_err()),
            [(3, "text_not_regular")]
        );

        // A symlinked router/, operator-turns/ or table supplies no authority.
        let routed = copy_of("valid");
        let moved = routed.path().join("moved");
        fs::rename(routed.path().join("router"), &moved).unwrap();
        symlink(&moved, routed.path().join("router")).unwrap();
        assert_eq!(
            codes(&load(routed.path()).unwrap_err()),
            [(0, "path_not_real")]
        );

        let linked = copy_of("valid");
        let real_dir = linked.path().join("real-texts");
        fs::rename(linked.path().join("router/operator-turns"), &real_dir).unwrap();
        symlink(&real_dir, linked.path().join("router/operator-turns")).unwrap();
        assert_eq!(
            codes(&load(linked.path()).unwrap_err()),
            [(0, "path_not_real")]
        );

        let table = copy_of("valid");
        let real_tsv = table.path().join("table.tsv");
        fs::rename(table.path().join(TSV_PATH), &real_tsv).unwrap();
        symlink(&real_tsv, table.path().join(TSV_PATH)).unwrap();
        assert_eq!(
            codes(&load(table.path()).unwrap_err()),
            [(0, "tsv_not_regular")]
        );
    }
}

#[test]
fn validate_takes_entries_without_a_filesystem() {
    let valid = tsv_text(&fixture("valid"));
    let text = |h: &str| Entry {
        name: format!("{h}.txt"),
        regular: true,
        bytes: fs::read(fixture("valid").join(format!("router/operator-turns/{h}.txt"))).unwrap(),
    };
    let tsv = Entry {
        name: TSV_PATH.into(),
        regular: true,
        bytes: valid.clone().into_bytes(),
    };
    let texts: Vec<Entry> = [H1, H2, H3, H4].iter().map(|h| text(h)).collect();
    assert_eq!(validate(&tsv, &texts).unwrap().len(), 7);
    // A tree entry that is a symlink (git mode 120000) is refused, whatever its bytes say.
    let mut linked = texts.clone();
    linked[1].regular = false;
    assert_eq!(
        codes(&validate(&tsv, &linked).unwrap_err()),
        [(3, "text_not_regular")]
    );
    let not_regular = Entry {
        regular: false,
        ..tsv.clone()
    };
    assert_eq!(
        codes(&validate(&not_regular, &texts).unwrap_err()),
        [(0, "tsv_not_regular")]
    );
}

#[test]
fn missing_tsv_is_unreadable() {
    let dir = tempfile::tempdir().unwrap();
    assert_eq!(codes(&load(dir.path()).unwrap_err()), [(0, "unreadable")]);
    fs::create_dir(dir.path().join("router")).unwrap();
    assert_eq!(codes(&load(dir.path()).unwrap_err()), [(0, "unreadable")]);
    fs::write(dir.path().join(TSV_PATH), format!("{HEADER}\n")).unwrap();
    assert!(load(dir.path()).unwrap().is_empty());
}

#[test]
fn checkout_with_crlf_is_restored_when_materialized() {
    let crlf = tempfile::tempdir().unwrap();
    let from = fixture("valid");
    for entry in walk(&from) {
        let rel = entry.strip_prefix(&from).unwrap();
        let target = crlf.path().join(rel);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        let text = String::from_utf8(fs::read(&entry).unwrap()).unwrap();
        fs::write(target, text.replace('\n', "\r\n")).unwrap();
    }
    let raw = fs::read_to_string(crlf.path().join(TSV_PATH)).unwrap();
    assert!(raw.contains("\r\n"));
    assert_eq!(codes(&load(crlf.path()).unwrap_err()), [(0, "line_ending")]);
    let restored = tempfile::tempdir().unwrap();
    copy_tree(crlf.path(), restored.path());
    assert_eq!(load(restored.path()).unwrap().len(), 7);
}

fn walk(dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            out.extend(walk(&path));
        } else {
            out.push(path);
        }
    }
    out
}

#[test]
fn relay_lifts_bind_to_their_authorizing_pins_of_one_turn() {
    let dir = copy_of("valid");
    let u1 = "00000000-0000-4000-8000-000000000001";
    let u2 = "00000000-0000-4000-8000-000000000002";
    let t = "2026-01-05T10:00:00.000Z";
    let lift = |item: &str| format!(r#"{{"item":"{item}","gate":"operator"}}"#);
    let z = format!("[{}]", lift("demo:Z"));
    let az = format!("[{},{}]", lift("demo:A"), lift("demo:Z"));
    // A second pin of the first human turn with another lift.
    append_row(
        dir.path(),
        &["1", H1, t, "human", "-", u1, "12", "D-111", &z, "-"],
    );
    // One relay of that turn whose lifts come from two pins of it (D-101 and D-111).
    let sources = format!("{H1}@{t}@D-101,{H1}@{t}@D-111");
    append_row(
        dir.path(),
        &[
            "1",
            H2,
            "2026-01-05T10:30:00.000Z",
            "relay",
            &sources,
            u2,
            "41",
            "D-112",
            &az,
            "-",
        ],
    );
    let rows = load(dir.path()).unwrap();
    assert_eq!(items(&rows, "D-112"), ["demo:A", "demo:Z"]);
    // Withdrawing D-111 revokes demo:Z only; the other source still authorizes demo:A.
    let text =
        tsv_text(dir.path()).replace(&format!("D-111\t{z}\t-"), &format!("D-111\t{z}\tD-113"));
    fs::write(dir.path().join(TSV_PATH), text).unwrap();
    let rows = load(dir.path()).unwrap();
    assert_eq!(items(&rows, "D-112"), ["demo:A"]);
    assert!(items(&rows, "D-111").is_empty());
    // A lift no named source holds is out of scope, and an unsorted list is refused.
    let wrong = tsv_text(dir.path()).replace(&format!("{H1}@{t}@D-101,"), "");
    fs::write(dir.path().join(TSV_PATH), wrong).unwrap();
    assert_eq!(codes(&load(dir.path()).unwrap_err()), [(10, "relay_scope")]);
    let unsorted = format!("{H1}@{t}@D-111,{H1}@{t}@D-101");
    let text = tsv_text(&fixture("valid"));
    let line3 = text
        .lines()
        .nth(2)
        .unwrap()
        .replace(&format!("{H1}@{t}@D-101"), &unsorted);
    let err = parse(&format!("{HEADER}\n{line3}\n")).unwrap_err();
    assert_eq!(codes(&err), [(2, "field:relay_of")]);
}

#[test]
fn blank_field_keeps_the_other_fields_of_the_row_checked() {
    let mut f = fields(2);
    f[5] = String::new();
    f[1] = H1.to_uppercase();
    f[9] = " D-200".into();
    let err = parse(&rows_of(&[f])).unwrap_err();
    assert_eq!(
        codes(&err),
        [
            (2, "field:session_uuid"),
            (2, "field:withdrawn_by"),
            (2, "field:turn_sha256")
        ]
    );
}

#[test]
fn d_row_citation_names_every_turn_of_a_decision() {
    let mut rows = valid_rows();
    assert_eq!(
        citation(&rows, "D-101"),
        Some(format!("operator-turns={H1}@2026-01-05T10:00:00.000Z"))
    );
    assert_eq!(citation(&rows, "D-999"), None);
    // D-6799 shape: one D-id pinned to two turns.
    let mut second = rows[0].clone();
    second.turn_sha256 = H2.into();
    second.utc_timestamp = "2026-01-05T10:30:00.000Z".into();
    rows.push(second);
    let cell = format!(
        "text before {} text after",
        citation(&rows, "D-101").unwrap()
    );
    let refs = cited_refs(&cell).unwrap().unwrap();
    assert_eq!(
        refs,
        [
            format!("{H2}@2026-01-05T10:30:00.000Z"),
            format!("{H1}@2026-01-05T10:00:00.000Z"),
        ]
    );
    assert_eq!(cited_refs("no statement here").unwrap(), None);
    let twice = format!("{cell} {}", citation(&rows, "D-101").unwrap());
    assert!(cited_refs(&twice).is_err());
    let descending =
        format!("operator-turns={H1}@2026-01-05T10:00:00.000Z,{H2}@2026-01-05T10:30:00.000Z");
    assert!(cited_refs(&descending).is_err());
    assert!(cited_refs("operator-turns=nothash@2026-01-05T10:00:00.000Z").is_err());
}

#[test]
fn relay_of_names_pins_of_one_human_turn() {
    let lifts = r#"[{"item":"demo:A","gate":"operator"},{"item":"demo:C","gate":"operator"}]"#;
    let relay = |sources: String| {
        let mut f = fields(3);
        f[2] = "2026-01-05T12:30:00.000Z".into();
        f[4] = sources;
        f[7] = "D-130".into();
        f[8] = lifts.into();
        f
    };
    // Two distinct turns whose references ascend both as text (4cd0... < a752...) and by D-id
    // (D-107 < D-111): only the one-turn rule can refuse the list.
    let mut h1_later = fields(2);
    h1_later[7] = "D-111".into();
    let ascending =
        format!("{H3}@2026-01-05T12:00:00.000Z@D-107,{H1}@2026-01-05T10:00:00.000Z@D-111");
    assert!(ascending.split(',').collect::<Vec<_>>().is_sorted());
    let err = parse(&rows_of(&[
        fields(2),
        fields(7),
        h1_later.clone(),
        relay(ascending),
    ]))
    .unwrap_err();
    assert_eq!(codes(&err), [(5, "field:relay_of")]);
    // The same turn twice in the list is no second pin of it: D-id order is strict.
    let same = format!("{H1}@2026-01-05T10:00:00.000Z@D-111,{H1}@2026-01-05T10:00:00.000Z@D-111");
    let err = parse(&rows_of(&[fields(2), h1_later, relay(same)])).unwrap_err();
    assert_eq!(codes(&err), [(4, "field:relay_of")]);
}

#[test]
fn a_relay_turn_may_be_pinned_again_to_other_source_pins() {
    let dir = copy_of("valid");
    let u1 = "00000000-0000-4000-8000-000000000001";
    let u2 = "00000000-0000-4000-8000-000000000002";
    let t = "2026-01-05T10:00:00.000Z";
    let z = r#"[{"item":"demo:Z","gate":"operator"}]"#;
    append_row(
        dir.path(),
        &["1", H1, t, "human", "-", u1, "12", "D-111", z, "-"],
    );
    // The relay turn of line 3 (H2 at 10:05, source D-101) gets a later decision on source D-111.
    let source = format!("{H1}@{t}@D-111");
    append_row(
        dir.path(),
        &[
            "1",
            H2,
            "2026-01-05T10:05:00.000Z",
            "relay",
            &source,
            u2,
            "40",
            "D-113",
            z,
            "-",
        ],
    );
    let rows = load(dir.path()).unwrap();
    assert_eq!(items(&rows, "D-113"), ["demo:Z"]);
    assert_eq!(items(&rows, "D-102"), ["demo:A"]);
    // Another session line is still a turn_conflict.
    let moved =
        tsv_text(dir.path()).replace(&format!("{u2}\t40\tD-113"), &format!("{u2}\t41\tD-113"));
    fs::write(dir.path().join(TSV_PATH), moved).unwrap();
    assert_eq!(
        codes(&load(dir.path()).unwrap_err()),
        [(10, "turn_conflict")]
    );
}

#[test]
fn unknown_kind_still_checks_relay_of() {
    for kind in ["", "operator"] {
        let mut f = fields(2);
        f[3] = kind.into();
        f[4] = "garbage".into();
        let err = parse(&rows_of(&[f])).unwrap_err();
        assert_eq!(
            codes(&err),
            [(2, "field:kind"), (2, "field:relay_of")],
            "{kind:?}"
        );
    }
    // A valid relay_of of an unknown kind reports the kind alone.
    let mut f = fields(3);
    f[3] = String::new();
    let err = parse(&rows_of(&[f])).unwrap_err();
    assert_eq!(codes(&err), [(2, "field:kind")]);
}

/// The committed blobs, not the checkout: `git ls-files --eol` reports the index side (`i/`).
/// A CRLF in a committed fixture would otherwise pass the tests, which restore CRLF on copy.
#[test]
fn committed_fixture_blobs_hold_no_crlf() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let listing = std::process::Command::new("git")
        .args(["ls-files", "--eol", "--", "tests/fixtures/operator_turns"])
        .current_dir(&root)
        .output();
    let Some(out) = listing.ok().filter(|o| o.status.success()) else {
        eprintln!("not a git work tree: committed blobs not checked");
        return;
    };
    let text = String::from_utf8(out.stdout).unwrap();
    assert!(text.lines().count() >= 8, "fixture listing: {text}");
    for line in text.lines() {
        let index = line.split_whitespace().next().unwrap();
        assert!(index == "i/lf" || index == "i/none", "{line}");
    }
}
