//! Reachable-history batch scanner with lossless, conservative blob carries.
use super::Refusal;
use std::{
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    path::Path,
    process::Stdio,
};
const CHUNK: usize = 65536;
const MAX_COMMIT_BYTES: usize = 1024 * 1024;

pub(super) fn reachable(root: &Path, input: &[u8]) -> Result<(), Refusal> {
    let run = || -> std::io::Result<_> {
        // File-backed stdin prevents a pipe deadlock while draining stdout.
        let mut file = tempfile::tempfile()?;
        file.write_all(input)?;
        file.seek(SeekFrom::Start(0))?;
        crate::workspace_resolver::git_command(root)
            .args([
                "--no-replace-objects",
                "-c",
                "core.hooksPath=",
                "cat-file",
                "--batch",
            ])
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_SYSTEM", "/dev/null")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .stdin(file)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
    };
    let mut child = run().map_err(|_| Refusal::BadBundle)?;
    let result = batch(
        &mut BufReader::new(child.stdout.take().ok_or(Refusal::BadBundle)?),
        input,
    );
    if result.is_err() {
        let _ = child.kill();
    }
    let status = child.wait().map_err(|_| Refusal::BadBundle)?;
    result?;
    if !status.success() {
        return Err(Refusal::BadBundle);
    }
    Ok(())
}

fn batch(reader: &mut impl BufRead, input: &[u8]) -> Result<(), Refusal> {
    let read = |_| Refusal::BadBundle;
    for oid in input.split(|b| *b == b'\n').filter(|s| !s.is_empty()) {
        let mut header = Vec::new();
        reader
            .take(256)
            .read_until(b'\n', &mut header)
            .map_err(read)?;
        if header.last() != Some(&b'\n') {
            return Err(Refusal::BadBundle);
        }
        let header = std::str::from_utf8(&header).map_err(|_| Refusal::BadBundle)?;
        let fields: Vec<_> = header.trim_end().split(' ').collect();
        if fields.len() != 3 || fields[0].as_bytes() != oid {
            return Err(Refusal::BadBundle);
        }
        let mut size: usize = fields[2].parse().map_err(|_| Refusal::BadBundle)?;
        // Scan the message as a borrowed slice of the sole commit-object buffer.
        if fields[1] == "commit" {
            if size > MAX_COMMIT_BYTES {
                return Err(Refusal::BadBundle);
            }
            let mut raw = vec![0; size];
            reader.read_exact(&mut raw).map_err(read)?;
            if crate::writer_secret::scan_bytes(
                crate::writer_secret::message(&raw).map_err(|_| Refusal::BadBundle)?,
            ) {
                return Err(Refusal::SecretInHistory);
            }
            size = 0;
        }
        if size > 0 {
            scan_chunks(reader, size, fields[1] == "blob")?;
        }
        let mut newline = [0];
        reader.read_exact(&mut newline).map_err(read)?;
        if newline != *b"\n" {
            return Err(Refusal::BadBundle);
        }
    }
    if !reader.fill_buf().map_err(read)?.is_empty() {
        return Err(Refusal::BadBundle);
    }
    Ok(())
}

fn scan_chunks(reader: &mut impl Read, mut size: usize, blob: bool) -> Result<(), Refusal> {
    let mut buffer = [0; CHUNK];
    let mut scanner = BlobScanner::default();
    let mut position = 0;
    while size > 0 {
        let count = size.min(CHUNK);
        reader
            .read_exact(&mut buffer[..count])
            .map_err(|_| Refusal::BadBundle)?;
        if blob && scanner.push(&buffer[..count], position) {
            return Err(Refusal::SecretInHistory);
        }
        position += count;
        size -= count;
    }
    Ok(())
}

/// Keep every possible start, not a fixed number of bytes. All policy matches
/// start with an ASCII word character or a PEM dash. Before the first such
/// character, only one character of boundary context is necessary. Once a
/// candidate exists we deliberately retain it through EOF: the shared policy
/// includes unbounded names, whitespace, quoted values and PEM headers.
/// This conservative strategy avoids duplicating the policy regexes here.
#[derive(Default)]
struct BlobScanner {
    text: [String; 5],
    started: [bool; 5],
    previous: Option<u8>,
}

impl BlobScanner {
    fn character(&mut self, lane: usize, c: u8) {
        if c.is_ascii_alphanumeric() || c == b'_' || c == b'-' {
            self.started[lane] = true;
        }
        if !self.started[lane] {
            self.text[lane].clear();
        }
        self.text[lane].push(char::from(c));
    }

    fn push(&mut self, bytes: &[u8], position: usize) -> bool {
        for (i, &byte) in bytes.iter().enumerate() {
            self.character(0, if byte < 128 { byte } else { b' ' });
            if let Some(previous) = self.previous {
                let alignment = (position + i - 1) % 2;
                for (endian, pair) in [[previous, byte], [byte, previous]].into_iter().enumerate() {
                    let value = u16::from_le_bytes(pair);
                    self.character(
                        1 + alignment * 2 + endian,
                        if value < 128 { value as u8 } else { b' ' },
                    );
                }
            }
            self.previous = Some(byte);
        }
        self.text
            .iter()
            .any(|text| crate::redact::redact(text) != *text)
    }
}
#[cfg(test)]
#[path = "finalize_scan_tests.rs"]
mod tests;
