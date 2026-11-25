//! Verified download: bytes land in `dest` only when size and sha256 match.
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::Path;

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// A lowercase SHA-256 hex digest: 64 characters of `0-9a-f` (the one validator for the crate).
pub fn valid_hash(hash: &str) -> bool {
    hash.len() == 64
        && hash
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub fn sha256_bytes(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
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

fn ureq_err(e: ureq::Error) -> String {
    match e {
        ureq::Error::BadUri(_) => "bad uri".into(),
        ureq::Error::RequireHttpsOnly(u) => {
            format!("configured for https only: {}", super::redact_url(&u))
        }
        _ => "request failed".into(),
    }
}

pub fn fetch(url: &str, dest: &Path, sha256: &str, size: Option<u64>) -> Result<(), String> {
    let tmp = dest.with_extension("part");
    let shown = super::redact_url(url);
    let result = (|| {
        let mut resp = ureq::get(url)
            .call()
            .map_err(|e| format!("download {shown}: {}", ureq_err(e)))?;
        let mut reader = resp.body_mut().with_config().limit(u64::MAX).reader();
        let mut out = std::fs::File::create(&tmp).map_err(|e| e.to_string())?;
        let mut buf = vec![0u8; 1 << 16];
        loop {
            let n = reader
                .read(&mut buf)
                .map_err(|e| format!("download {shown}: {e}"))?;
            if n == 0 {
                break;
            }
            out.write_all(&buf[..n]).map_err(|e| e.to_string())?;
        }
        out.flush().map_err(|e| e.to_string())?;
        verify(&tmp, sha256, size).map_err(|e| format!("refuse {shown}: {e}"))
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
    use std::io::{BufRead, BufReader};
    use std::net::TcpListener;

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

    #[test]
    fn failing_fetch_redacts_userinfo_and_query_from_error() {
        let d = tempfile::tempdir().unwrap();
        let dest = d.path().join("pkg.tar.gz");

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let err = fetch(
            &format!("https://alice:pwsecret@{addr}/a/b.tgz?sig=signedval&exp=999"),
            &dest,
            &"0".repeat(64),
            None,
        )
        .unwrap_err();
        assert!(err.contains(&format!("{addr}/a/b.tgz")), "err={err}");
        for secret in ["alice", "pwsecret", "sig=", "signedval", "exp=", "?"] {
            assert!(!err.contains(secret), "leaked {secret} in {err}");
        }

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(&stream);
            let mut line = String::new();
            while reader.read_line(&mut line).unwrap() > 0 {
                if line == "\r\n" || line == "\n" {
                    break;
                }
                line.clear();
            }
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nabc")
                .unwrap();
        });
        let err = fetch(
            &format!("http://alice:pwsecret@{addr}/a/b.tgz?sig=signedval"),
            &dest,
            &"0".repeat(64),
            None,
        )
        .unwrap_err();
        handle.join().unwrap();
        assert!(
            err.starts_with(&format!("refuse {addr}/a/b.tgz:")),
            "err={err}"
        );
        for secret in ["alice", "pwsecret", "sig=", "signedval", "?"] {
            assert!(!err.contains(secret), "leaked {secret} in {err}");
        }
    }

    #[test]
    fn ureq_err_redacts_other_variants_to_generic_message() {
        let secret_err = ureq::Error::Io(std::io::Error::other(
            "https://alice:pwsecret@example.com/a/b.tgz?sig=signedval",
        ));
        let msg = ureq_err(secret_err);
        assert_eq!(msg, "request failed");
        for secret in ["alice", "pwsecret", "sig=", "signedval", "example.com", "?"] {
            assert!(!msg.contains(secret), "leaked {secret} in {msg}");
        }
        assert_eq!(ureq_err(ureq::Error::StatusCode(404)), "request failed");
    }
}
