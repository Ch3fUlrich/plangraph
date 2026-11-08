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
    while size > 0 {
        let count = size.min(CHUNK);
        reader
            .read_exact(&mut buffer[..count])
            .map_err(|_| Refusal::BadBundle)?;
        if blob && scanner.push(&buffer[..count]) {
            return Err(Refusal::SecretInHistory);
        }
        size -= count;
    }
    if blob && scanner.finish() {
        return Err(Refusal::SecretInHistory);
    }
    Ok(())
}

/// Retain raw bytes without dropping starts or boundary context. Use the shared
/// policy, including all four UTF-16 lanes, rather than duplicating its rules.
/// Scan only after the carry doubles and at EOF: total input to the policy is
/// O(blob size), not O(blob size squared / CHUNK). The carry costs O(blob size)
/// memory; Vec capacity can approach 2N for N retained bytes. The shared scanner
/// decodes five lanes totaling at most 3N bytes (N ASCII + four N/2 UTF-16),
/// sequentially, not simultaneously: at most N ASCII plus N/2 UTF-16 text is
/// live, in addition to redaction's linear temporary strings. Thus peak memory
/// is O(N), not a bounded window. No size cap adds secret refusals.
#[derive(Default)]
struct BlobScanner {
    bytes: Vec<u8>,
    scanned: usize,
    #[cfg(test)]
    scanned_bytes: usize,
}

impl BlobScanner {
    fn scan(&mut self) -> bool {
        self.scanned = self.bytes.len();
        #[cfg(test)]
        {
            self.scanned_bytes += self.scanned;
        }
        crate::writer_secret::scan_bytes(&self.bytes)
    }

    fn push(&mut self, bytes: &[u8]) -> bool {
        self.bytes.extend_from_slice(bytes);
        if self.bytes.len() >= self.scanned.max(CHUNK).saturating_mul(2) {
            self.scan()
        } else {
            false
        }
    }

    fn finish(&mut self) -> bool {
        self.bytes.len() != self.scanned && self.scan()
    }
}
#[cfg(test)]
#[path = "finalize_scan_tests.rs"]
mod tests;
