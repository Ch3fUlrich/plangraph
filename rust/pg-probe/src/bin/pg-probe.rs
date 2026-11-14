use pg_probe::{
    runner::{digest, rescore, run_batch, Observation, Pinned},
    SourceStatus,
};
use std::{
    collections::BTreeMap,
    fs,
    io::{self, Write},
    path::Path,
};
fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() != 5 || !matches!(args[0].as_str(), "run" | "daily" | "rescore") {
        return Err("usage: pg-probe run|daily PINNED.json PROBES.jsonl SOURCES.json OUTPUT; or rescore RUN.json PROBES.jsonl SOURCES.json OUTPUT.json".into());
    }
    let config_bytes = fs::read(&args[1])?;
    let fixture_bytes = fs::read(&args[2])?;
    let source_bytes = fs::read(&args[3])?;
    let previous: Option<serde_json::Value> = if args[0] == "rescore" {
        Some(serde_json::from_slice(&config_bytes)?)
    } else {
        None
    };
    let pinned: Pinned = if let Some(previous) = &previous {
        serde_json::from_value(previous["pinned"].clone())?
    } else {
        serde_json::from_slice(&config_bytes)?
    };
    let probes = pg_probe::load(io::Cursor::new(&fixture_bytes))?;
    if probes.is_empty() {
        return Err("empty probe inventory".into());
    }
    let sources: BTreeMap<String, SourceStatus> = serde_json::from_slice(&source_bytes)?;
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?;
    let output_path = if args[0] == "daily" {
        Path::new(&args[4]).join(format!("run-{}.json", stamp.as_nanos()))
    } else {
        Path::new(&args[4]).to_path_buf()
    };
    let output = output_path.as_path();
    if output.exists() {
        return Err("refusing to overwrite an existing run".into());
    }
    let parent = output
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(parent)?;
    let scratch = tempfile::tempdir_in(parent)?;
    let started = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs();
    let rows = if let Some(previous) = previous {
        let observations: Vec<Observation> =
            serde_json::from_value(previous["observations"].clone())?;
        rescore(&probes, &sources, &pinned.config, &observations)?
    } else {
        run_batch(&probes, &sources, &pinned, scratch.path())?
    };
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer(
        &mut file,
        &serde_json::json!({
            "schema_version":1,"started":started,"config_sha256":digest(&config_bytes),
            "fixture_sha256":digest(&fixture_bytes),"sources_sha256":digest(&source_bytes),
            "rescored_from_sha256":if args[0]=="rescore" {Some(digest(&config_bytes))}else{None},
            "pinned":pinned,"probes":probes,"sources":sources,"observations":rows
        }),
    )?;
    file.write_all(b"\n")?;
    file.as_file().sync_all()?;
    file.persist_noclobber(output)?;
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("pg-probe: {error}");
        std::process::exit(1);
    }
}
