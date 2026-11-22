//! Read-only comparison with an independently supplied build of main.
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    path::Path,
};

fn hex(input: &str, len: usize) -> bool {
    input.len() == len && input.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn regular(path: &Path) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NONBLOCK);
    }
    let file = options.open(path)?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::other("not a regular file"));
    }
    Ok(file)
}

fn stamp(path: &Path) -> Option<String> {
    let mut value = String::new();
    regular(path)
        .ok()?
        .take(66)
        .read_to_string(&mut value)
        .ok()?;
    let sha = value.trim_end_matches(['\n', '\r']);
    (hex(sha, 40) || hex(sha, 64)).then(|| sha.to_ascii_lowercase())
}

fn checksum(path: &Path) -> Option<String> {
    let mut file = regular(path).ok()?;
    let mut hash = Sha256::new();
    io::copy(&mut file, &mut hash).ok()?;
    Some(format!("{:x}", hash.finalize()))
}

pub(crate) fn execute(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    let mut flags = std::collections::BTreeMap::new();
    let (pairs, remaining) = args.as_chunks::<2>();
    for pair in pairs {
        if !matches!(
            pair[0].as_str(),
            "--host-id" | "--binary" | "--source-stamp" | "--main-sha" | "--main-sha256"
        ) || pair[1].is_empty()
            || flags.insert(pair[0].as_str(), pair[1].as_str()).is_some()
        {
            return Err("check-install: invalid or duplicate flag".into());
        }
    }
    if !remaining.is_empty() || flags.len() != 5 {
        return Err("Usage: pg-spawnd check-install --host-id ID --binary PATH --source-stamp PATH --main-sha SHA --main-sha256 SHA256".into());
    }
    let host = flags["--host-id"];
    let main = flags["--main-sha"];
    let expected_sum = flags["--main-sha256"];
    if !pg_worker_core::fleet_series::valid_host(host)
        || !(hex(main, 40) || hex(main, 64))
        || !hex(expected_sum, 64)
    {
        return Err("check-install: invalid host id or main build identity".into());
    }
    let stamp_path = Path::new(flags["--source-stamp"]);
    let recorded = stamp(stamp_path);
    let measured = checksum(Path::new(flags["--binary"]));
    let reason = match (&recorded, &measured) {
        (None, _) => "source_stamp_unavailable",
        (_, None) => "binary_unreadable",
        (Some(sha), _) if stamp(stamp_path).as_ref() != Some(sha) => "source_stamp_changed",
        (Some(sha), _) if !sha.eq_ignore_ascii_case(main) => "source_sha_mismatch",
        (_, Some(sum)) if !sum.eq_ignore_ascii_case(expected_sum) => "binary_sha256_mismatch",
        _ => "build_matches",
    };
    let status = match reason {
        "build_matches" => "current",
        "source_sha_mismatch" | "binary_sha256_mismatch" => "stale",
        _ => "unknown",
    };
    writeln!(
        out,
        "{}",
        json!({
            "event": "pg_spawnd_install", "host": host, "status": status, "reason": reason,
            "source_sha": recorded, "sha256": measured, "main_sha": main,
            "main_sha256": expected_sum,
        })
    )
    .map_err(|error| error.to_string())
}
