use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::{Seek, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const FLOOR: u64 = 19096;
fn sequence(path: &Path) -> Option<(u64, u64)> {
    if path.extension()? != "dump" {
        return None;
    }
    let stem = path.file_stem()?.to_str()?;
    let (seq, stamp) = stem.strip_prefix("keeper-schema-seq")?.split_once('-')?;
    Some((seq.parse().ok()?, stamp.parse().ok()?))
}
fn archives(dir: &Path) -> Result<Vec<(u64, PathBuf)>> {
    let mut result = Vec::new();
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        if let Some((seq, stamp)) = sequence(&entry.path()) {
            if entry.file_type()?.is_file() {
                result.push((stamp, (seq, entry.path())));
            }
        }
    }
    result.sort();
    Ok(result.into_iter().map(|(_, entry)| entry).collect())
}
fn publish(dir: &Path, seq: u64, bytes: &[u8]) -> Result<PathBuf> {
    if seq < FLOOR || bytes.is_empty() {
        return Err("backup below sequence floor or empty".into());
    }
    fs::create_dir_all(dir)?;
    let stamp = u64::try_from(
        chrono::Utc::now()
            .timestamp_nanos_opt()
            .ok_or("invalid clock")?,
    )?;
    let path = dir.join(format!("keeper-schema-seq{seq}-{stamp}.dump"));
    let mut file = tempfile::NamedTempFile::new_in(dir)?;
    file.write_all(bytes)?;
    file.as_file().sync_all()?;
    let ledger = dir.join("ledger.jsonl");
    if !ledger.exists() {
        tempfile::NamedTempFile::new_in(dir)?.persist_noclobber(&ledger)?;
    }
    if fs::symlink_metadata(&ledger)?.file_type().is_symlink() {
        return Err("unsafe ledger".into());
    }
    let mut log = OpenOptions::new().append(true).open(ledger)?;
    writeln!(
        log,
        "{}",
        json!({"seq":seq,"size":bytes.len(),"sha256":format!("{:x}", Sha256::digest(bytes))})
    )?;
    log.sync_all()?;
    file.persist_noclobber(&path)?;
    fs::File::open(dir)?.sync_all()?;
    let entries = archives(dir)?;
    for (_, old) in entries.iter().take(entries.len().saturating_sub(14)) {
        fs::remove_file(old)?;
    }
    Ok(path)
}
fn checked_archive(path: &Path) -> Result<Vec<u8>> {
    if !fs::symlink_metadata(path)?.file_type().is_file() {
        return Err("unsafe archive".into());
    }
    let (seq, _) = sequence(path).ok_or("invalid archive name")?;
    if seq < FLOOR {
        return Err("restore refuses sequence below 19096".into());
    }
    let bytes = fs::read(path)?;
    let dir = path.parent().ok_or("archive directory required")?;
    let ledger = dir.join("ledger.jsonl");
    if !fs::symlink_metadata(&ledger)?.is_file() {
        return Err("unsafe ledger".into());
    }
    let ledger = fs::read_to_string(ledger)?;
    let hash = format!("{:x}", Sha256::digest(&bytes));
    let valid = ledger
        .lines()
        .filter_map(|s| serde_json::from_str::<Value>(s).ok())
        .any(|row| {
            row["seq"].as_u64() == Some(seq)
                && row["size"].as_u64() == Some(bytes.len() as u64)
                && row["sha256"].as_str() == Some(hash.as_str())
        });
    if !valid {
        return Err("archive ledger mismatch".into());
    }
    Ok(bytes)
}
fn docker(args: &[&str], input: Option<&[u8]>) -> Result<Vec<u8>> {
    let mut file = tempfile::tempfile()?;
    if let Some(bytes) = input {
        file.write_all(bytes)?;
        file.rewind()?;
    }
    let output = Command::new("docker")
        .args(args)
        .stdin(Stdio::from(file))
        .stderr(Stdio::null())
        .output()?;
    if !output.status.success() {
        return Err("PostgreSQL backup command failed".into());
    }
    Ok(output.stdout)
}
fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if !cfg!(unix) || args.len() != 3 {
        return Err("usage on Unix: keeper-backup dump DIR | restore-test ARCHIVE".into());
    }
    match args[1].as_str() {
        "dump" => {
            let schema = std::env::var("PGSCHEMA")?;
            if !regex::Regex::new(r"^[a-zA-Z_][a-zA-Z0-9_]*$")?.is_match(&schema) {
                return Err("invalid PGSCHEMA".into());
            }
            let common: Vec<_> = "run --rm --network host -e PGHOST -e PGPORT -e PGUSER -e PGPASSWORD -e PGDATABASE -e PGSSLMODE postgres:17".split_whitespace().collect();
            let sql = format!("SELECT COALESCE(MAX(seq),0) FROM \"{schema}\".events");
            let mut query = common.to_vec();
            query.extend(["psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-c", &sql]);
            let seq: u64 = String::from_utf8(docker(&query, None)?)?.trim().parse()?;
            if seq < FLOOR {
                return Err("source sequence below floor".into());
            }
            let mut dump = common.to_vec();
            dump.extend("pg_dump --format=custom --no-owner --no-acl --schema".split_whitespace());
            dump.push(&schema);
            publish(Path::new(&args[2]), seq, &docker(&dump, None)?)?;
        }
        "restore-test" => restore_test(Path::new(&args[2]))?,
        _ => return Err("unknown backup verb".into()),
    }
    Ok(())
}
fn scratch(sql: &str, input: Option<&[u8]>) -> Result<Vec<u8>> {
    let mut args: Vec<_> = "run --rm -i --network none --user postgres --tmpfs /scratch:mode=1777 --entrypoint bash postgres:17 -ceu".split_whitespace().collect();
    args.push(sql);
    docker(&args, input)
}
fn archive(flags: &str, bytes: &[u8]) -> Result<Vec<u8>> {
    let args = format!("run --rm -i --network none postgres:17 pg_restore {flags}");
    docker(&args.split_whitespace().collect::<Vec<_>>(), Some(bytes))
}
fn restore_test(path: &Path) -> Result<()> {
    let bytes = checked_archive(path)?;
    let data = archive("--data-only --table=events --file=-", &bytes)?;
    let maximum = String::from_utf8(data)?
        .lines()
        .filter_map(|line| line.split('\t').next()?.parse::<u64>().ok())
        .max()
        .unwrap_or(0);
    if maximum < FLOOR {
        return Err("archive state below sequence floor".into());
    }
    let listing = archive("-l", &bytes)?;
    if String::from_utf8(listing)?
        .lines()
        .filter(|s| s.contains(" TABLE ") && !s.contains(" TABLE DATA "))
        .count()
        < 40
    {
        return Err("expected at least 40 keeper tables".into());
    }
    scratch("initdb -D /scratch/db -A trust >/dev/null; pg_ctl -D /scratch/db -o '-k /scratch -c listen_addresses=' -w start >/dev/null; pg_restore --exit-on-error --no-owner --no-acl -h /scratch -d postgres", Some(&bytes))?;
    Ok(())
}
#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn backup_floor_and_archive_integrity() {
        let dir = tempfile::tempdir().unwrap();
        let old = dir.path().join("keeper-schema-seq19095-1.dump");
        fs::write(&old, b"archive").unwrap();
        assert!(checked_archive(&old).is_err());
        assert!(publish(dir.path(), 19095, b"archive").is_err());
        let bad = tempfile::tempdir().unwrap();
        fs::create_dir(bad.path().join("ledger.jsonl")).unwrap();
        assert!(publish(bad.path(), FLOOR, b"archive").is_err());
        assert!(archives(bad.path()).unwrap().is_empty());
        let path = publish(dir.path(), 19109, b"archive").unwrap();
        assert_eq!(checked_archive(&path).unwrap(), b"archive");
        std::fs::write(&path, b"changed").unwrap();
        assert!(checked_archive(&path).is_err());
    }
    #[test]
    #[ignore = "requires local postgres:17 image and Docker; isolated containers only"]
    fn backup_restore_scratch() {
        let output = scratch(
            "timeout 5 sh -c 'head -c 8388608 /dev/zero; cat >/dev/null'",
            Some(&vec![0; 8388608]),
        )
        .unwrap();
        assert_eq!(output.len(), 8388608);
        let archive = |seq| {
            scratch(&format!("initdb -D /scratch/db -A trust >/dev/null; pg_ctl -D /scratch/db -o '-k /scratch -c listen_addresses=' -w start >/dev/null; for i in $(seq 1 42); do psql -h /scratch -d postgres -qc \"CREATE TABLE t$i (id int)\"; done; psql -h /scratch -d postgres -qc \"CREATE TABLE events(seq bigint); INSERT INTO events VALUES ({seq})\"; pg_dump -h /scratch -d postgres -Fc"), None).unwrap()
        };
        let dir = tempfile::tempdir().unwrap();
        let old = publish(dir.path(), FLOOR, &archive(FLOOR - 1)).unwrap();
        assert!(restore_test(&old).is_err());
        let path = publish(dir.path(), FLOOR, &archive(FLOOR)).unwrap();
        restore_test(&path).unwrap();
    }
    #[test]
    fn backup_private_retention_and_ledger() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        for seq in (19100..19115).rev() {
            publish(dir.path(), seq, b"archive").unwrap();
        }
        let path = archives(dir.path()).unwrap().last().unwrap().1.clone();
        assert_eq!(
            std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(archives(dir.path()).unwrap()[0].0, 19113);
        assert_eq!(archives(dir.path()).unwrap().len(), 14);
        let ledger = fs::read_to_string(dir.path().join("ledger.jsonl")).unwrap();
        assert_eq!(ledger.lines().count(), 15);
    }
}
fn main() {
    if run().is_err() {
        eprintln!("keeper-backup failed: verify source, archive and container");
        std::process::exit(1);
    }
}
