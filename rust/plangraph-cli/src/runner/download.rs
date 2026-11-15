//! Verified download: bytes land in `dest` only when size and sha256 match.
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::Path;

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn sha256_file(path: &Path) -> Result<String, String> {
    let mut f = std::fs::File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 16];
    loop {
        let n = f
            .read(&mut buf)
            .map_err(|e| format!("{}: {e}", path.display()))?;
        if n == 0 {
            return Ok(hex(&h.finalize()));
        }
        h.update(&buf[..n]);
    }
}

pub fn verify(path: &Path, sha256: &str, size: Option<u64>) -> Result<(), String> {
    if let Some(want) = size {
        let have = std::fs::metadata(path).map_err(|e| e.to_string())?.len();
        if have != want {
            return Err(format!("size {have} != pinned {want}"));
        }
    }
    let have = sha256_file(path)?;
    if have != sha256 {
        return Err(format!("sha256 {have} != pinned {sha256}"));
    }
    Ok(())
}

pub fn fetch(url: &str, dest: &Path, sha256: &str, size: Option<u64>) -> Result<(), String> {
    let tmp = dest.with_extension("part");
    let result = (|| {
        let mut resp = ureq::get(url)
            .call()
            .map_err(|e| format!("download {url}: {e}"))?;
        let mut reader = resp.body_mut().with_config().limit(u64::MAX).reader();
        let mut out = std::fs::File::create(&tmp).map_err(|e| e.to_string())?;
        let mut buf = vec![0u8; 1 << 16];
        loop {
            let n = reader
                .read(&mut buf)
                .map_err(|e| format!("download {url}: {e}"))?;
            if n == 0 {
                break;
            }
            out.write_all(&buf[..n]).map_err(|e| e.to_string())?;
        }
        out.flush().map_err(|e| e.to_string())?;
        verify(&tmp, sha256, size).map_err(|e| format!("refuse {url}: {e}"))
    })();
    match result {
        Ok(()) => std::fs::rename(&tmp, dest).map_err(|e| e.to_string()),
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            Err(e)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn verify_accepts_matching_and_refuses_size_or_sha_mismatch() {
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("f");
        std::fs::write(&f, b"abc").unwrap();
        let sha = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
        assert!(verify(&f, sha, Some(3)).is_ok());
        assert!(verify(&f, sha, Some(4)).unwrap_err().contains("size"));
        assert!(verify(&f, &"0".repeat(64), None)
            .unwrap_err()
            .contains("sha256"));
    }
}
