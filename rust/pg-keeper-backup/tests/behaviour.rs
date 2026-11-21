use pg_keeper_backup::pg::{count_tables, redact, Pg, ScratchState};
use pg_keeper_backup::{ledger, names, restore_test, retention, run_dump, Verify};
use std::cell::RefCell;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const NOW: u64 = 1_794_000_000; // 2026-11-06T21:20Z

/// A dump name built at run time: `seq` at `NOW` plus `day` days.
fn dump_at(seq: u64, day: u64) -> String {
    names::dump_name(seq, NOW + day * 86_400)
}

/// The name of a dump of `seq` taken at `NOW`, spelled out from fragments instead of
/// the production formatter, so a formatter bug cannot cancel out in an assertion.
fn literal_name(seq: u64) -> String {
    [
        "keeper-schema-seq",
        &seq.to_string(),
        "-",
        &stamp_of_now(),
        ".dump",
    ]
    .concat()
}

fn stamp_of_now() -> String {
    ["2026", "11", "06", "T", "2120", "Z"].concat()
}

fn path_at(seq: u64, day: u64) -> PathBuf {
    Path::new("/x").join(dump_at(seq, day))
}

struct Fake {
    seq: u64,
    tables: usize,
    scratch_seq: u64,
    collide: bool,
    calls: RefCell<Vec<String>>,
}

impl Fake {
    fn new(seq: u64) -> Self {
        Self {
            seq,
            tables: 43,
            scratch_seq: seq,
            collide: false,
            calls: RefCell::new(Vec::new()),
        }
    }
    fn log(&self, what: &str) {
        self.calls.borrow_mut().push(what.to_owned());
    }
}

fn listing(tables: usize) -> String {
    let mut text = String::from(";\n; Archive created at 2026-11-13\n");
    for i in 0..tables {
        text += &format!("{}; 1259 {} TABLE keeper t{i} keeper\n", 200 + i, 16000 + i);
        text += &format!(
            "{}; 0 {} TABLE DATA keeper t{i} keeper\n",
            300 + i,
            16000 + i
        );
    }
    text += "9; 2615 1 SCHEMA - keeper keeper\n";
    text
}

impl Pg for Fake {
    fn max_seq(&self, _: &str) -> Result<u64, String> {
        self.log("max_seq");
        Ok(self.seq)
    }
    fn dump(&self, _: &str, out: &mut dyn Write) -> Result<(), String> {
        self.log("dump");
        out.write_all(b"PGDMP-fake").map_err(|e| e.to_string())
    }
    fn list(&self, _: &Path) -> Result<String, String> {
        self.log("list");
        Ok(listing(self.tables))
    }
    fn create_scratch(&self, _: &str) -> Result<(), String> {
        self.log("create");
        if self.collide {
            return Err("database already exists".into());
        }
        Ok(())
    }
    fn load_scratch(&self, _: &Path, _: &str) -> Result<(), String> {
        self.log("restore");
        Ok(())
    }
    fn scratch_state(&self, _: &str, _: &str) -> Result<ScratchState, String> {
        Ok(ScratchState {
            tables: self.tables,
            max_seq: self.scratch_seq,
        })
    }
    fn drop_scratch(&self, _: &str) -> Result<(), String> {
        self.log("drop");
        Ok(())
    }
}

fn mode(path: &Path) -> u32 {
    std::fs::metadata(path).unwrap().permissions().mode() & 0o777
}

#[test]
fn stamp_is_utc_minute_resolution() {
    assert_eq!(names::stamp(0), "19700101T0000Z");
    assert_eq!(names::stamp(1_794_000_000), "20261106T2120Z");
    assert_eq!(names::stamp(1_709_164_800 + 86_399), "20240229T2359Z");
}

#[test]
fn dump_name_round_trips_and_rejects_other_names() {
    let name = names::dump_name(19109, NOW);
    assert_eq!(name, "keeper-schema-seq19109-20261106T2120Z.dump");
    assert_eq!(names::parse(&name), Some((19109, "20261106T2120Z")));
    for bad in [
        "keeper-schema-ledger.txt",
        "keeper-schema-seq-20261106T2120Z.dump",
        "keeper-schema-seq1-2026.dump",
        "keeper-schema-seq1x-20261106T2120Z.dump",
        "keeper-schema-seq1-20261106T2120Z.dump.part",
        "keeper.db",
    ] {
        assert_eq!(names::parse(bad), None, "{bad}");
    }
}

#[test]
fn a_dump_run_writes_a_0600_file_and_one_ledger_line() {
    let dir = tempfile::tempdir().unwrap();
    let out = run_dump(&Fake::new(19200), dir.path(), "keeper", 14, NOW, None).unwrap();
    let file = dir.path().join(&out.entry.file);
    assert_eq!(std::fs::read(&file).unwrap(), b"PGDMP-fake");
    assert_eq!(mode(&file), 0o600);
    assert_eq!(mode(&dir.path().join(ledger::LEDGER_NAME)), 0o600);
    let text = std::fs::read_to_string(dir.path().join(ledger::LEDGER_NAME)).unwrap();
    assert_eq!(text.lines().count(), 1);
    // sha256 of "PGDMP-fake"
    let want = format!(
        "{} seq=19200 size=10 sha256={} verified=skipped file={}",
        stamp_of_now(),
        out.entry.sha256,
        literal_name(19200)
    );
    assert_eq!(text.trim(), want);
    assert_eq!(out.entry.sha256.len(), 64);
    let leftovers: Vec<_> = std::fs::read_dir(dir.path())
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    assert_eq!(leftovers.len(), 2, "no .part file stays: {leftovers:?}");
}

#[test]
fn retention_deletes_the_fifteenth_oldest_and_keeps_fourteen() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("notes.txt"), "keep me").unwrap();
    let fake = Fake::new(19200);
    let mut first = String::new();
    for day in 0..15u64 {
        let out = run_dump(&fake, dir.path(), "keeper", 14, NOW + day * 86_400, None).unwrap();
        if day == 0 {
            first = out.entry.file.clone();
        }
        assert_eq!(out.pruned.len(), usize::from(day == 14), "day {day}");
        if day == 14 {
            assert_eq!(out.pruned, vec![first.clone()]);
        }
    }
    let mut dumps = 0;
    for e in std::fs::read_dir(dir.path()).unwrap() {
        let name = e.unwrap().file_name().into_string().unwrap();
        dumps += usize::from(names::parse(&name).is_some());
    }
    assert_eq!(dumps, 14);
    assert!(!dir.path().join(&first).exists());
    assert!(dir.path().join("notes.txt").exists());
    // the ledger keeps its history
    let ledger = std::fs::read_to_string(dir.path().join(ledger::LEDGER_NAME)).unwrap();
    assert_eq!(ledger.lines().count(), 15);
}

#[test]
fn retention_plan_orders_by_stamp_then_seq_and_ignores_foreign_names() {
    let names_in = |v: &[&str]| v.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
    let all = names_in(&[
        &dump_at(19109, 0),
        &dump_at(19300, 2),
        &dump_at(19200, 1),
        "other.dump",
    ]);
    assert_eq!(retention::plan(&all, 2), vec![dump_at(19109, 0)]);
    assert!(retention::plan(&all, 3).is_empty());
    assert!(retention::plan(&all, 0).len() == 3);
}

#[test]
fn a_failed_dump_leaves_no_file_and_no_ledger_line() {
    struct Broken(Fake);
    impl Pg for Broken {
        fn max_seq(&self, s: &str) -> Result<u64, String> {
            self.0.max_seq(s)
        }
        fn dump(&self, _: &str, out: &mut dyn Write) -> Result<(), String> {
            out.write_all(b"half").unwrap();
            Err("pg_dump failed".into())
        }
        fn list(&self, d: &Path) -> Result<String, String> {
            self.0.list(d)
        }
        fn create_scratch(&self, s: &str) -> Result<(), String> {
            self.0.create_scratch(s)
        }
        fn load_scratch(&self, d: &Path, s: &str) -> Result<(), String> {
            self.0.load_scratch(d, s)
        }
        fn scratch_state(&self, s: &str, c: &str) -> Result<ScratchState, String> {
            self.0.scratch_state(s, c)
        }
        fn drop_scratch(&self, s: &str) -> Result<(), String> {
            self.0.drop_scratch(s)
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let err = run_dump(
        &Broken(Fake::new(19200)),
        dir.path(),
        "keeper",
        14,
        NOW,
        None,
    )
    .err()
    .unwrap();
    assert_eq!(err, "pg_dump failed");
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
}

#[test]
fn restore_test_refuses_a_dump_older_than_seq_19096_before_touching_postgres() {
    let fake = Fake::new(19096);
    let old = path_at(19095, 0);
    let err = restore_test(&fake, &old, "keeper", 43, names::MIN_SEQ)
        .err()
        .unwrap();
    assert!(err.contains("older than 19096"), "{err}");
    assert!(fake.calls.borrow().is_empty());
}

#[test]
fn restore_test_passes_on_a_fresh_dump_and_drops_the_scratch() {
    let fake = Fake::new(19096);
    let name = path_at(19096, 0);
    let report = restore_test(&fake, &name, "keeper", 43, names::MIN_SEQ).unwrap();
    assert_eq!((report.seq, report.tables), (19096, 43));
    assert_eq!(*fake.calls.borrow(), ["list", "create", "restore", "drop"]);
}

#[test]
fn restore_test_rejects_wrong_table_count_and_stale_content_and_still_drops() {
    let name = path_at(19200, 0);
    let mut few = Fake::new(19200);
    few.tables = 42;
    assert!(restore_test(&few, &name, "keeper", 43, names::MIN_SEQ)
        .unwrap_err()
        .contains("42 tables"));
    // a renamed old dump: the scratch content is older than the name says
    let mut renamed = Fake::new(19200);
    renamed.scratch_seq = 18000;
    let err = restore_test(&renamed, &name, "keeper", 43, names::MIN_SEQ).unwrap_err();
    assert!(err.contains("below"), "{err}");
    assert_eq!(
        renamed.calls.borrow().last().map(String::as_str),
        Some("drop")
    );
}

#[test]
fn restore_test_only_accepts_dump_names() {
    let fake = Fake::new(19200);
    assert!(restore_test(
        &fake,
        Path::new("/x/keeper.db"),
        "keeper",
        43,
        names::MIN_SEQ
    )
    .is_err());
    assert!(restore_test(&fake, Path::new("/x/a.dump"), "Keeper;", 43, names::MIN_SEQ).is_err());
}

#[test]
fn table_count_ignores_data_and_other_schemas() {
    let mut text = listing(43);
    text += "1; 1259 5 TABLE public other keeper\n";
    assert_eq!(count_tables(&text, "keeper"), 43);
}

#[test]
fn the_password_never_appears_in_text_or_arguments() {
    assert_eq!(
        redact("auth failed for s3cret on host", &["s3cret".to_owned()]),
        "auth failed for *** on host"
    );
    assert_eq!(redact("unchanged", &[String::new()]), "unchanged");
    let args = pg_keeper_backup::docker::run_args(
        "postgres:17",
        Some(Path::new("/b")),
        &["pg_dump", "-Fc"],
    );
    assert!(args.windows(2).any(|w| w == ["-e", "PGPASSWORD"]));
    assert!(
        args.windows(2).any(|w| w == ["--log-driver", "none"]),
        "docker must keep no log copy"
    );
    assert!(
        args.iter()
            .all(|a| !a.contains('=') || a.starts_with("/b:")),
        "{args:?}"
    );
    assert!(args.contains(&"/b:/backup:ro".to_owned()));
}

const VERIFY: Verify = Verify {
    tables: 43,
    min_seq: names::MIN_SEQ,
};

#[test]
fn a_name_collision_never_drops_a_database_this_run_did_not_create() {
    let mut fake = Fake::new(19200);
    fake.collide = true;
    let name = path_at(19200, 0);
    assert!(restore_test(&fake, &name, "keeper", 43, names::MIN_SEQ).is_err());
    assert_eq!(*fake.calls.borrow(), ["list", "create"]);
}

#[test]
fn a_dump_that_fails_its_restore_test_is_quarantined_and_prunes_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let good = Fake::new(19200);
    for day in 0..14u64 {
        run_dump(
            &good,
            dir.path(),
            "keeper",
            14,
            NOW + day * 86_400,
            Some(&VERIFY),
        )
        .unwrap();
    }
    let mut bad = Fake::new(19300);
    bad.tables = 42;
    let err = run_dump(
        &bad,
        dir.path(),
        "keeper",
        14,
        NOW + 14 * 86_400,
        Some(&VERIFY),
    )
    .err()
    .unwrap();
    assert!(err.contains("restore test failed"), "{err}");
    let all: Vec<String> = std::fs::read_dir(dir.path())
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    assert_eq!(all.iter().filter(|n| names::parse(n).is_some()).count(), 14);
    assert_eq!(all.iter().filter(|n| n.ends_with(".unverified")).count(), 1);
    let ledger = std::fs::read_to_string(dir.path().join(ledger::LEDGER_NAME)).unwrap();
    assert!(ledger.lines().last().unwrap().contains("verified=no"));
    assert_eq!(
        ledger
            .lines()
            .filter(|l| l.contains("verified=yes"))
            .count(),
        14
    );
}

#[test]
fn unverified_dumps_are_capped_and_never_counted_as_backups() {
    let all: Vec<String> = (1..=5u64)
        .map(|d| format!("{}{}", dump_at(19200, d), ".unverified"))
        .collect();
    assert_eq!(retention::plan(&all, 14), all[..2].to_vec());
}

#[test]
fn a_second_run_in_the_same_minute_does_not_overwrite_the_first_dump() {
    let dir = tempfile::tempdir().unwrap();
    let fake = Fake::new(19200);
    let first = run_dump(&fake, dir.path(), "keeper", 14, NOW, None).unwrap();
    let before = std::fs::read(dir.path().join(&first.entry.file)).unwrap();
    let err = run_dump(&fake, dir.path(), "keeper", 14, NOW + 20, None)
        .err()
        .unwrap();
    assert!(err.contains("already exists"), "{err}");
    assert_eq!(
        std::fs::read(dir.path().join(&first.entry.file)).unwrap(),
        before
    );
    let ledger = std::fs::read_to_string(dir.path().join(ledger::LEDGER_NAME)).unwrap();
    assert_eq!(ledger.lines().count(), 1);
    assert!(std::fs::read_dir(dir.path()).unwrap().all(|e| !e
        .unwrap()
        .file_name()
        .to_str()
        .unwrap()
        .ends_with(".part")));
}

fn files(dir: &Path) -> Vec<String> {
    let mut all: Vec<String> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    all.sort();
    all
}

#[test]
fn a_failed_candidate_never_has_a_dump_name_and_an_existing_quarantine_is_not_overwritten() {
    let dir = tempfile::tempdir().unwrap();
    let mut bad = Fake::new(19200);
    bad.tables = 42;
    run_dump(&bad, dir.path(), "keeper", 14, NOW, Some(&VERIFY))
        .err()
        .unwrap();
    let kept = dir
        .path()
        .join(format!("{}{}", dump_at(19200, 0), ".unverified"));
    std::fs::write(&kept, "first").unwrap();
    let err = run_dump(&bad, dir.path(), "keeper", 14, NOW + 10, Some(&VERIFY))
        .err()
        .unwrap();
    assert!(err.contains("already exists"), "{err}");
    assert_eq!(std::fs::read_to_string(&kept).unwrap(), "first");
    let names = files(dir.path());
    assert!(
        names.iter().all(|n| names::parse(n).is_none()),
        "no .dump name: {names:?}"
    );
    assert!(names.iter().all(|n| !n.ends_with(".part")), "{names:?}");
    let ledger = std::fs::read_to_string(dir.path().join(ledger::LEDGER_NAME)).unwrap();
    assert_eq!(ledger.lines().count(), 1, "one record for one path");
}

#[test]
fn failed_restores_keep_only_three_quarantined_dumps_and_every_verified_one() {
    let dir = tempfile::tempdir().unwrap();
    let good = Fake::new(19200);
    for day in 0..14u64 {
        run_dump(
            &good,
            dir.path(),
            "keeper",
            14,
            NOW + day * 86_400,
            Some(&VERIFY),
        )
        .unwrap();
    }
    let mut bad = Fake::new(19300);
    bad.tables = 42;
    for day in 14..19u64 {
        run_dump(
            &bad,
            dir.path(),
            "keeper",
            14,
            NOW + day * 86_400,
            Some(&VERIFY),
        )
        .err()
        .unwrap();
    }
    let names = files(dir.path());
    assert_eq!(
        names.iter().filter(|n| names::parse(n).is_some()).count(),
        14
    );
    assert_eq!(
        names.iter().filter(|n| n.ends_with(".unverified")).count(),
        3
    );
}

#[test]
fn a_ledger_failure_publishes_no_archive_under_a_dump_name() {
    let dir = tempfile::tempdir().unwrap();
    // A directory where the ledger file should be makes every append fail.
    std::fs::create_dir(dir.path().join(ledger::LEDGER_NAME)).unwrap();
    let err = run_dump(&Fake::new(19200), dir.path(), "keeper", 14, NOW, None)
        .err()
        .unwrap();
    assert!(
        err.contains("ledger") && err.contains("not published"),
        "{err}"
    );
    assert_eq!(files(dir.path()), [ledger::LEDGER_NAME]);
}
