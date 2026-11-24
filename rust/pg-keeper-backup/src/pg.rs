//! The Postgres side, behind one trait so every rule is tested without a server.

use std::io::{self, BufRead, Write};
use std::path::Path;

/// What a restored scratch database holds.
#[derive(Debug, PartialEq, Eq)]
pub struct ScratchState {
    pub tables: usize,
    pub max_seq: u64,
}

/// What the live keeper schema holds, read in one SELECT.
#[derive(Debug, PartialEq, Eq)]
pub struct LiveState {
    /// Highest `events.seq`.
    pub seq: u64,
    /// Base tables of the schema in `information_schema.tables`.
    pub tables: usize,
}

pub trait Pg {
    /// Highest `events.seq` and the table count of the live keeper schema, one read.
    fn live_state(&self, schema: &str) -> Result<LiveState, String>;
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
    let schema_prefix = format!("COPY {schema}.");
    let quoted_schema_prefix = format!("COPY \"{schema}\".");
    let mut line = Vec::new();
    let mut highest = 0;
    let mut seq_column = None;
    let mut complete = false;
    while script.read_until(b'\n', &mut line)? != 0 {
        let bytes = line.strip_suffix(b"\n").unwrap_or(&line);
        if let Some(column) = seq_column {
            if bytes == b"\\." {
                seq_column = None;
                complete = true;
            } else {
                let seq = bytes
                    .split(|&byte| byte == b'\t')
                    .nth(column)
                    .unwrap_or_default();
                let number = std::str::from_utf8(seq)
                    .ok()
                    .and_then(|seq| seq.parse::<u64>().ok())
                    .ok_or_else(|| invalid("invalid event sequence in archive"))?;
                highest = highest.max(number);
            }
        } else if let Some(columns) = bytes
            .strip_prefix(schema_prefix.as_bytes())
            .or_else(|| bytes.strip_prefix(quoted_schema_prefix.as_bytes()))
            .and_then(|table| {
                table
                    .strip_prefix(b"events (")
                    .or_else(|| table.strip_prefix(b"\"events\" ("))
            })
        {
            if complete {
                return Err(invalid("unexpected event COPY header in archive"));
            }
            let columns = columns
                .strip_suffix(b") FROM stdin;")
                .and_then(|columns| std::str::from_utf8(columns).ok())
                .ok_or_else(|| invalid("unexpected event COPY header in archive"))?;
            let mut quoted = false;
            let column = columns
                .split(|ch| {
                    if ch == '"' {
                        quoted = !quoted;
                    }
                    ch == ',' && !quoted
                })
                .position(|column| matches!(column.trim(), "seq" | "\"seq\""))
                .ok_or_else(|| invalid("event COPY header has no seq column"))?;
            seq_column = Some(column);
        }
        line.clear();
    }
    if !complete || seq_column.is_some() {
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

#[cfg(test)]
mod tests {
    use super::read_archive_seq;
    use std::io::ErrorKind;

    #[test]
    fn archive_sequence_follows_copy_column_order() {
        let script = b"COPY keeper.events (payload, seq, hash) FROM stdin;\nrow\t19200\ta\nother\t19100\tb\n\\.\n";
        assert_eq!(read_archive_seq(&script[..], "keeper").unwrap(), 19200);

        let quoted =
            b"COPY \"keeper\".\"events\" (\"long,name\", \"seq\") FROM stdin;\nrow\t19300\n\\.\n";
        assert_eq!(read_archive_seq(&quoted[..], "keeper").unwrap(), 19300);
    }

    #[test]
    fn archive_sequence_refuses_malformed_copy() {
        for script in [
            "COPY keeper.events (payload, hash) FROM stdin;\nrow\t19200\n\\.\n",
            "COPY keeper.events (payload, seq) FROM stdin;\n19200\n\\.\n",
            "COPY keeper.events (payload, seq) FROM stdin;\nrow\tinvalid\n\\.\n",
            "COPY keeper.events (payload, seq) FROM stdin;\nrow\t19200\n",
        ] {
            let error = read_archive_seq(script.as_bytes(), "keeper").unwrap_err();
            assert_eq!(error.kind(), ErrorKind::InvalidData, "{script}");
        }
    }
}
