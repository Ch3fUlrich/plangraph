//! Scheduled keeper schema dump with retention, a ledger and a restore test.
//!
//! `dump` writes `keeper-schema-seq<SEQ>-<stamp>.dump` (mode 0600), appends one ledger
//! line (seq, size, sha256), then keeps the newest N dumps. `restore-test` refuses any
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
use std::path::{Path, PathBuf};

pub const KEEP: usize = 14;
pub const TABLES: usize = 43;

pub struct DumpOutcome {
    pub entry: ledger::Entry,
    pub pruned: Vec<String>,
}

/// One dump run: dump, fsync, rename into place, ledger line, retention.
pub fn run_dump(
    pg: &dyn Pg,
    dir: &Path,
    schema: &str,
    keep: usize,
    now: u64,
) -> Result<DumpOutcome, String> {
    check_ident(schema)?;
    fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let seq = pg.max_seq(schema)?;
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
    let (sha256, size) = match result {
        Ok((_, 0)) => return discard(&part, "pg_dump wrote nothing".into()),
        Ok(done) => done,
        Err(error) => return discard(&part, error),
    };
    let final_path = dir.join(&name);
    fs::File::open(&part)
        .and_then(|f| f.sync_all())
        .map_err(|e| e.to_string())?;
    fs::set_permissions(&part, fs::Permissions::from_mode(0o600)).map_err(|e| e.to_string())?;
    fs::rename(&part, &final_path).map_err(|e| e.to_string())?;
    let entry = ledger::Entry {
        stamp: names::stamp(now),
        seq,
        size,
        sha256,
        file: name,
    };
    ledger::append(dir, &entry).map_err(|e| e.to_string())?;
    let pruned = retention::prune(dir, keep).map_err(|e| e.to_string())?;
    Ok(DumpOutcome { entry, pruned })
}

fn discard<T>(part: &PathBuf, error: String) -> Result<T, String> {
    let _ = fs::remove_file(part);
    Err(error)
}

#[derive(Debug)]
pub struct RestoreReport {
    pub seq: u64,
    pub tables: usize,
}

/// Refuse a dump older than `min_seq`; list it; load it into a scratch database.
pub fn restore_test(
    pg: &dyn Pg,
    dump: &Path,
    schema: &str,
    expected_tables: usize,
    min_seq: u64,
) -> Result<RestoreReport, String> {
    check_ident(schema)?;
    let name = dump
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or("dump name")?;
    let (seq, _) =
        names::parse(name).ok_or_else(|| format!("{name}: not a keeper schema dump name"))?;
    if seq < min_seq {
        return Err(format!("{name}: seq {seq} is older than {min_seq}; keeper state that old is never restored (D-5796)"));
    }
    let tables = count_tables(&pg.list(dump)?, schema);
    if tables != expected_tables {
        return Err(format!(
            "{name}: listing has {tables} tables, expected {expected_tables}"
        ));
    }
    let scratch = format!("pgkb_scratch_{seq}_{}", std::process::id());
    let loaded = pg
        .restore_scratch(dump, &scratch)
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
    if state.max_seq < seq.max(min_seq) {
        return Err(format!(
            "{name}: scratch max seq {} is below the dump name's seq {seq}",
            state.max_seq
        ));
    }
    Ok(RestoreReport {
        seq: state.max_seq,
        tables: state.tables,
    })
}
