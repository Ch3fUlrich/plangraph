//! The coverage gate: a unit file under `deploy/` or a binary target of a workspace crate that
//! `deploy/manifest.toml` does not name fails here, in the existing `cargo test` job.
use plangraph_cli::deploy_manifest::{scan, Manifest};
use std::collections::BTreeSet;
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
fn deploy_manifest_declares_every_path_a_unit_runs_a_binary_from_every_env_file_and_every_installer(
) {
    let repo = repo();
    let manifest = Manifest::load_from_repo(&repo).unwrap();
    let units = scan::unit_files(&repo).unwrap();
    let mut problems = manifest.consumer_problems(&scan::consumers(&repo, &units).unwrap());
    problems.extend(manifest.env_problems(&scan::unit_env_files(&repo, &units).unwrap()));
    problems.extend(manifest.installer_problems(&repo, &units));
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

fn roles(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|r| (*r).to_owned()).collect()
}

#[test]
fn deploy_manifest_pg_spawnd_paths_follow_host_roles() {
    let repo = repo();
    let manifest = Manifest::load_from_repo(&repo).unwrap();
    let entries: Vec<_> = manifest
        .binary
        .iter()
        .filter(|b| b.package == "pg-spawnd" && b.bin == "pg-spawnd")
        .collect();
    assert_eq!(
        entries.len(),
        1,
        "one entry carries every path of pg-spawnd"
    );
    let spawnd = entries[0];
    // The user-bin copy serves every role, the system copy only the WSL hosts.
    for role in ["keeper", "worker", "wsl"] {
        assert!(spawnd
            .install_paths_on(&roles(&[role]))
            .contains(&"~/.local/bin/pg-spawnd"));
    }
    assert_eq!(
        spawnd.install_paths_on(&roles(&["wsl"])),
        ["~/.local/bin/pg-spawnd", "/usr/local/bin/pg-spawnd"]
    );
    assert!(!spawnd
        .install_paths_on(&roles(&["keeper"]))
        .contains(&"/usr/local/bin/pg-spawnd"));
    // The units that run it say the same: the agents view (keeper) from the user bin directory, the
    // WSL cache guard (system unit on wsl) from /usr/local/bin.
    let units = scan::unit_files(&repo).unwrap();
    let found = scan::consumers(&repo, &units).unwrap();
    let path_of = |unit: &str| {
        found
            .iter()
            .find(|c| c.unit == unit && c.bin == "pg-spawnd")
            .map(|c| c.path.as_str())
    };
    assert_eq!(
        path_of("systemd/plangraph-agents-view.service"),
        Some("~/.local/bin/pg-spawnd")
    );
    assert_eq!(
        path_of("systemd/pg-wsl-cache-guard.service"),
        Some("/usr/local/bin/pg-spawnd")
    );
}

#[test]
fn deploy_manifest_installed_names_of_template_families_differ_from_their_sources() {
    let repo = repo();
    let manifest = Manifest::load_from_repo(&repo).unwrap();
    let unit = |name: &str| manifest.unit.iter().find(|u| u.name == name).unwrap();
    let ctx = unit("ctx-watchdog");
    assert_eq!(ctx.enable_unit().unwrap(), "ctx-watchdog.timer.in");
    assert_eq!(ctx.installed_enable_unit().unwrap(), "ctx-watchdog.timer");
    assert_eq!(
        ctx.installed_service_unit().as_deref(),
        Some("ctx-watchdog.service")
    );
    let stall = unit("stall-watchdog");
    assert_eq!(stall.enable_unit().unwrap(), "watchdog.timer");
    assert_eq!(
        stall.installed_enable_unit().unwrap(),
        "plangraph-stall-watchdog.timer"
    );
    assert_eq!(
        stall.installed_service_unit().as_deref(),
        Some("plangraph-stall-watchdog.service")
    );
}

#[test]
fn deploy_manifest_stall_watchdog_without_its_installer_fails_the_installer_check() {
    let repo = repo();
    let mut manifest = Manifest::load_from_repo(&repo).unwrap();
    let units = scan::unit_files(&repo).unwrap();
    assert!(manifest.installer_problems(&repo, &units).is_empty());
    manifest
        .unit
        .iter_mut()
        .find(|u| u.name == "stall-watchdog")
        .unwrap()
        .installer = None;
    let problems = manifest.installer_problems(&repo, &units);
    assert!(
        problems
            .iter()
            .any(|p| p.contains("deploy/watchdog/watchdog.service")),
        "{problems:?}"
    );
}
