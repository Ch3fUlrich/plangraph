//! Reachable-history batch scanner; commits use one object buffer, blobs a fixed window.
use super::Refusal;
use std::{
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    path::Path,
    process::Stdio,
};
const CHUNK: usize = 65536;
const OVERLAP: usize = 16384;
const MAX_COMMIT_BYTES: usize = 1024 * 1024;
// Cross-edge secret matches longer than OVERLAP are out of scope: real credentials
// are far shorter. Patterns remain unchanged, but streaming coverage is capped.

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
    let mut buffer = [0; CHUNK + OVERLAP];
    let mut kept = 0;
    while size > 0 {
        let count = size.min(CHUNK);
        reader
            .read_exact(&mut buffer[kept..kept + count])
            .map_err(|_| Refusal::BadBundle)?;
        let end = kept + count;
        if blob && crate::writer_secret::scan_bytes(&buffer[..end]) {
            return Err(Refusal::SecretInHistory);
        }
        kept = end.min(OVERLAP);
        buffer.copy_within(end - kept..end, 0);
        size -= count;
    }
    Ok(())
}
#[cfg(test)]
#[path = "finalize_scan_tests.rs"]
mod tests;
