//! The Postgres side, behind one trait so every rule is tested without a server.

use std::io::{self, BufRead, Write};
use std::path::Path;

/// What a restored scratch database holds.
#[derive(Debug, PartialEq, Eq)]
pub struct ScratchState {
    pub tables: usize,
    pub max_seq: u64,
}

pub trait Pg {
    /// Highest `events.seq` of the live keeper schema.
    fn max_seq(&self, schema: &str) -> Result<u64, String>;
    /// Stream a custom-format `pg_dump` of the schema into `out`.
    fn dump(&self, schema: &str, out: &mut dyn Write) -> Result<(), String>;
    /// `pg_restore -l` of a dump file.
    fn list(&self, dump: &Path) -> Result<String, String>;
    /// Highest archived `events.seq`, inspected without connecting to Postgres.
    fn archive_seq(&self, dump: &Path, schema: &str) -> Result<u64, String>;
    /// Create the scratch database; fails when the name is taken, so a database this run
    /// did not create is never touched afterwards.
    fn create_scratch(&self, scratch: &str) -> Result<(), String>;
    /// Load the dump into the scratch database.
    fn load_scratch(&self, dump: &Path, scratch: &str) -> Result<(), String>;
    fn scratch_state(&self, scratch: &str, schema: &str) -> Result<ScratchState, String>;
    fn drop_scratch(&self, scratch: &str) -> Result<(), String>;
}

/// Scan the `pg_restore --data-only --table events` script without executing it.
pub(crate) fn read_archive_seq(mut script: impl BufRead, schema: &str) -> io::Result<u64> {
    let invalid = |message| io::Error::new(io::ErrorKind::InvalidData, message);
    let target = format!("COPY {schema}.events (");
    let quoted = format!("COPY \"{schema}\".events (");
    let mut line = Vec::new();
    let mut highest = 0;
    let mut copying = false;
    let mut complete = false;
    while script.read_until(b'\n', &mut line)? != 0 {
        let bytes = line.strip_suffix(b"\n").unwrap_or(&line);
        if copying {
            if bytes == b"\\." {
                copying = false;
                complete = true;
            } else {
                let seq = bytes
                    .split(|&byte| byte == b'\t')
                    .next()
                    .unwrap_or_default();
                let number = std::str::from_utf8(seq)
                    .ok()
                    .and_then(|seq| seq.parse::<u64>().ok())
                    .ok_or_else(|| invalid("invalid event sequence in archive"))?;
                highest = highest.max(number);
            }
        } else if let Some(columns) = bytes
            .strip_prefix(target.as_bytes())
            .or_else(|| bytes.strip_prefix(quoted.as_bytes()))
        {
            if complete || !columns.starts_with(b"seq,") || !columns.ends_with(b") FROM stdin;") {
                return Err(invalid("unexpected event COPY header in archive"));
            }
            copying = true;
        }
        line.clear();
    }
    if !complete || copying {
        return Err(invalid("incomplete event COPY in archive"));
    }
    Ok(highest)
}

/// A schema or scratch name that is safe inside double quotes.
pub fn check_ident(name: &str) -> Result<&str, String> {
    let ok = !name.is_empty()
        && name.len() <= 63
        && name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
    if ok {
        Ok(name)
    } else {
        Err(format!(
            "invalid postgres name {name:?} (lowercase letters, digits, _)"
        ))
    }
}

/// Tables of `schema` in a `pg_restore -l` listing (`TABLE DATA` lines excluded).
pub fn count_tables(listing: &str, schema: &str) -> usize {
    listing
        .lines()
        .filter(|l| !l.starts_with(';'))
        .filter(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            f.len() > 5 && f[3] == "TABLE" && f[4] == schema
        })
        .count()
}

/// Replace every secret value in `text` by `***`.
pub fn redact(text: &str, secrets: &[String]) -> String {
    secrets
        .iter()
        .filter(|s| !s.is_empty())
        .fold(text.to_owned(), |t, s| t.replace(s.as_str(), "***"))
}
