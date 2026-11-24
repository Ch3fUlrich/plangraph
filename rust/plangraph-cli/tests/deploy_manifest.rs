//! The coverage gate: a unit file under `deploy/` or a binary target of a workspace crate that
//! `deploy/manifest.toml` does not name fails here, in the existing `cargo test` job.
use plangraph_cli::deploy_manifest::{scan, Manifest};
use std::path::{Path, PathBuf};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

#[test]
fn every_unit_file_and_binary_target_is_in_the_manifest() {
    let repo = repo();
    let manifest = Manifest::load_from_repo(&repo).unwrap();
    let problems = manifest.coverage(
        &scan::unit_files(&repo).unwrap(),
        &scan::bin_targets(&repo).unwrap(),
    );
    assert!(problems.is_empty(), "\n{}", problems.join("\n"));
}

#[test]
fn the_scan_sees_the_known_targets() {
    let repo = repo();
    let units = scan::unit_files(&repo).unwrap();
    assert!(units.contains("pg-feeder.timer") && units.contains("pg-host-ssh-link@.service"));
    assert!(units.contains("pganchor/pganchor.socket"));
    assert!(!units.iter().any(|u| u.ends_with(".example")));
    let bins = scan::bin_targets(&repo).unwrap();
    for (package, bin) in [
        ("pg-spawnd", "pg-spawnd"),
        ("plangraph-cli", "plangraph"),
        ("plangraph-cli", "host-sync"),
        ("pg-worker-core", "pg-disk-hygiene"),
        ("pg-hook", "commit-msg"),
    ] {
        assert!(
            bins.contains(&(package.into(), bin.into())),
            "{package}/{bin} not found in {bins:?}"
        );
    }
}
