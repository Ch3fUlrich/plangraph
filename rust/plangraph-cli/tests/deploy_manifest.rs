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
fn deploy_manifest_names_every_unit_file_and_binary_target() {
    let repo = repo();
    let manifest = Manifest::load_from_repo(&repo).unwrap();
    let problems = manifest.coverage(
        &scan::unit_files(&repo).unwrap(),
        &scan::bin_targets(&repo).unwrap(),
    );
    assert!(problems.is_empty(), "\n{}", problems.join("\n"));
}

#[test]
fn deploy_manifest_declares_every_path_a_unit_runs_a_binary_from_and_every_env_file() {
    let repo = repo();
    let manifest = Manifest::load_from_repo(&repo).unwrap();
    let units = scan::unit_files(&repo).unwrap();
    let mut problems = manifest.consumer_problems(&scan::consumer_paths(&repo, &units).unwrap());
    problems.extend(manifest.env_problems(&scan::unit_env_files(&repo, &units).unwrap()));
    assert!(problems.is_empty(), "\n{}", problems.join("\n"));
}

#[test]
fn deploy_manifest_scan_sees_the_known_targets() {
    let repo = repo();
    let units = scan::unit_files(&repo).unwrap();
    assert!(
        units.contains("systemd/pg-feeder.timer")
            && units.contains("systemd/pg-host-ssh-link@.service")
            && units.contains("pganchor/pganchor.socket")
    );
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
