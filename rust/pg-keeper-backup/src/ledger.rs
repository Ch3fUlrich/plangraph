//! One ledger line per dump: `<stamp> seq=<n> size=<bytes> sha256=<hex> file=<name>`.

use sha2::{Digest, Sha256};
use std::fs::OpenOptions;
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

pub const LEDGER_NAME: &str = "keeper-schema-ledger.txt";

#[derive(Debug, PartialEq, Eq)]
pub struct Entry {
    pub stamp: String,
    pub seq: u64,
    pub size: u64,
    pub sha256: String,
    pub file: String,
}

impl Entry {
    pub fn line(&self) -> String {
        format!(
            "{} seq={} size={} sha256={} file={}",
            self.stamp, self.seq, self.size, self.sha256, self.file
        )
    }
}

/// A writer that hashes and counts what passes through it.
pub struct Hashing<W: Write> {
    inner: W,
    hash: Sha256,
    pub size: u64,
}

impl<W: Write> Hashing<W> {
    pub fn new(inner: W) -> Self {
        Self {
            inner,
            hash: Sha256::new(),
            size: 0,
        }
    }

    pub fn finish(mut self) -> io::Result<(String, u64)> {
        self.inner.flush()?;
        let digest = self.hash.finalize();
        Ok((
            digest.iter().map(|b| format!("{b:02x}")).collect(),
            self.size,
        ))
    }
}

impl<W: Write> Write for Hashing<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.hash.update(&buf[..n]);
        self.size += n as u64;
        Ok(n)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

/// Append one line; the ledger is created with mode 0600.
pub fn append(dir: &Path, entry: &Entry) -> io::Result<()> {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(dir.join(LEDGER_NAME))?;
    writeln!(file, "{}", entry.line())
}
