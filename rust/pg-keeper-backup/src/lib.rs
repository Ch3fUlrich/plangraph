//! Scheduled keeper schema dump with retention, a ledger and a restore test.
//!
//! `dump` writes `keeper-schema-seq<SEQ>-<stamp>.dump` (mode 0600), appends one ledger
//! line (seq, size, sha256), then (after the optional restore test) keeps the newest N dumps. `restore-test` refuses any
//! dump below [`names::MIN_SEQ`] (D-5796), lists the dump and loads it into a scratch
//! database, which is dropped again. The live schema is only ever read.

pub mod docker;
pub mod ledger;
pub mod names;
pub mod pg;
pub mod retention;

use pg::{check_ident, count_tables, Pg};
use std::fs::{self, OpenOptions};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;

pub const KEEP: usize = 14;

pub struct DumpOutcome {
    pub entry: ledger::Entry,
    pub pruned: Vec<String>,
}

/// Restore-test settings for a dump run: the seq floor and an optional table count.
/// Without `tables` the run expects the count it read from the live catalog together
/// with the dump's seq.
pub struct Verify {
    pub tables: Option<usize>,
    pub min_seq: u64,
}

/// One dump run. The dump is written to a `.part` file that is verified (with `verify`)
/// BEFORE it gets any retention-eligible name:
///
/// * passed (or no verification asked): published as `<name>.dump` by hard link, which
///   refuses to overwrite, then a ledger line, then retention;
/// * failed: published as `<name>.dump.unverified` (also never overwriting), a
///   `verified=no` line, the oldest `.unverified` beyond the cap pruned, verified dumps
///   untouched, and the run fails. A bad dump can never evict a verified one.
pub fn run_dump(
    pg: &dyn Pg,
    dir: &Path,
    schema: &str,
    keep: usize,
    now: u64,
    verify: Option<&Verify>,
) -> Result<DumpOutcome, String> {
    check_ident(schema)?;
    fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let live = pg.live_state(schema)?;
    let seq = live.seq;
    let name = names::dump_name(seq, now);
    let part = dir.join(format!(".{name}.part"));
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&part)
        .map_err(|e| format!("{}: {e}", part.display()))?;
    let mut out = ledger::Hashing::new(file);
    let result = pg
        .dump(schema, &mut out)
        .and_then(|()| out.finish().map_err(|e| e.to_string()));
    let outcome = match result {
        Ok((_, 0)) => Err("pg_dump wrote nothing".to_owned()),
        Ok((sha256, size)) => publish(
            pg,
            dir,
            &part,
            &name,
            seq,
            now,
            (sha256, size),
            schema,
            verify,
            live.tables,
        ),
        Err(error) => Err(error),
    };
    let _ = fs::remove_file(&part);
    let (entry, failure) = outcome?;
    if let Some(error) = failure {
        // Quarantine only: verified backups are never pruned for a bad dump.
        let _ = retention::prune(dir, usize::MAX);
        return Err(format!(
            "restore test failed, dump kept as {}: {error}",
            entry.file
        ));
    }
    let pruned = retention::prune(dir, keep).map_err(|e| e.to_string())?;
    Ok(DumpOutcome { entry, pruned })
}

type Published = (ledger::Entry, Option<String>);

#[allow(clippy::too_many_arguments)]
fn publish(
    pg: &dyn Pg,
    dir: &Path,
    part: &Path,
    name: &str,
    seq: u64,
    now: u64,
    (sha256, size): (String, u64),
    schema: &str,
    verify: Option<&Verify>,
    live_tables: usize,
) -> Result<Published, String> {
    fs::File::open(part)
        .and_then(|f| f.sync_all())
        .and_then(|()| fs::set_permissions(part, fs::Permissions::from_mode(0o600)))
        .map_err(|e| e.to_string())?;
    let failure = match verify {
        Some(v) => check_dump(
            pg,
            part,
            name,
            seq,
            schema,
            Some(v.tables.unwrap_or(live_tables)),
            v.min_seq,
        )
        .err(),
        None => None,
    };
    let verified = match (verify, &failure) {
        (None, _) => "skipped",
        (Some(_), None) => "yes",
        (Some(_), Some(_)) => "no",
    };
    let file = if failure.is_some() {
        format!("{name}{}", names::UNVERIFIED)
    } else {
        name.to_owned()
    };
    let target = dir.join(&file);
    if target.exists() {
        return Err(format!(
            "{}: a dump of seq {seq} already exists for this minute",
            target.display()
        ));
    }
    let entry = ledger::Entry {
        stamp: names::stamp(now),
        seq,
        size,
        sha256,
        file,
        verified,
    };
    // Ledger line first: an archive never gets a retention-eligible name without its
    // record, whatever interrupts the run. (A line without a file is harmless.)
    ledger::append(dir, &entry).map_err(|e| format!("ledger: {e}; dump not published"))?;
    fs::hard_link(part, &target).map_err(|e| {
        format!(
            "{}: {e} (ledger line for {} has no file)",
            target.display(),
            entry.file
        )
    })?;
    fs::File::open(dir)
        .and_then(|d| d.sync_all())
        .map_err(|e| format!("{}: {e}", dir.display()))?;
    Ok((entry, failure))
}

#[derive(Debug)]
pub struct RestoreReport {
    pub seq: u64,
    pub tables: usize,
}

/// Refuse a dump older than `min_seq`; list it; load it into a scratch database.
/// `expected_tables` of `None` reads the live catalog's table count (read-only), after
/// the seq floor has refused an old dump.
pub fn restore_test(
    pg: &dyn Pg,
    dump: &Path,
    schema: &str,
    expected_tables: Option<usize>,
    min_seq: u64,
) -> Result<RestoreReport, String> {
    check_ident(schema)?;
    let name = dump
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or("dump name")?;
    let (seq, _) =
        names::parse(name).ok_or_else(|| format!("{name}: not a keeper schema dump name"))?;
    check_dump(pg, dump, name, seq, schema, expected_tables, min_seq)
}

/// The restore test of the file `dump`, labelled `name` and claimed to hold state `seq`.
fn check_dump(
    pg: &dyn Pg,
    dump: &Path,
    name: &str,
    seq: u64,
    schema: &str,
    expected_tables: Option<usize>,
    min_seq: u64,
) -> Result<RestoreReport, String> {
    if seq < min_seq {
        return Err(format!("{name}: seq {seq} is older than {min_seq}; keeper state that old is never restored (D-5796)"));
    }
    let expected_tables = match expected_tables {
        Some(n) => n,
        None => pg.live_state(schema)?.tables,
    };
    let tables = count_tables(&pg.list(dump)?, schema);
    if tables != expected_tables {
        return Err(format!(
            "{name}: listing has {tables} tables, expected {expected_tables}"
        ));
    }
    let archived_seq = pg.archive_seq(dump, schema)?;
    if archived_seq < seq.max(min_seq) {
        return Err(format!(
            "{name}: archive max seq {archived_seq} is below the dump name's seq {seq}"
        ));
    }
    let scratch = format!("pgkb_scratch_{seq}_{}", std::process::id());
    // Only a database this run created is ever dropped: a name collision fails here.
    pg.create_scratch(&scratch)?;
    let loaded = pg
        .load_scratch(dump, &scratch)
        .and_then(|()| pg.scratch_state(&scratch, schema));
    let dropped = pg.drop_scratch(&scratch);
    let state = loaded?;
    dropped?;
    if state.tables != expected_tables {
        return Err(format!(
            "{name}: scratch holds {} tables, expected {expected_tables}",
            state.tables
        ));
    }
    if state.max_seq != archived_seq {
        return Err(format!(
            "{name}: scratch max seq {} differs from archive max seq {archived_seq}",
            state.max_seq
        ));
    }
    Ok(RestoreReport {
        seq: state.max_seq,
        tables: state.tables,
    })
}
