//! The `pg-disk-hygiene` CLI against a main checkout's `rust/target` and the legacy target containers.
#![cfg(target_os = "linux")]
#[path = "support/disk_hygiene.rs"]
mod support;
use std::{
    fs,
    process::Command,
    time::{Duration, SystemTime},
};
use support::{fixture, git};

fn age_unrelated_target(root: &std::path::Path) {
    // A protected main target can coexist with successful reclamation on a hot test device.
    let old = SystemTime::now() - Duration::from_secs(2 * 86400);
    for path in ["clean/target/cache", "clean/target"] {
        fs::File::open(root.join(path))
            .unwrap()
            .set_modified(old)
            .unwrap();
    }
}

/// Hosts run `pg-disk-hygiene` from a user unit whose `WorkingDirectory` is the main checkout:
/// the unit pins the checkout, yet its idle `rust/target` must still be removed (and one the
/// unit names itself must not).
#[test]
fn a_unit_pinning_the_checkout_root_does_not_stop_the_idle_main_target_sweep() {
    for pin_target_itself in [false, true] {
        let (tmp, repo, root) = fixture();
        age_unrelated_target(&root);
        let target = repo.join("rust/target");
        fs::write(repo.join(".git/info/exclude"), "rust/target/\n").unwrap();
        fs::create_dir_all(target.join("debug")).unwrap();
        fs::write(target.join("debug/blob"), [0u8; 64]).unwrap();
        fs::write(
            target.join("CACHEDIR.TAG"),
            b"Signature: 8a477f597d28d172789f06886806bc55",
        )
        .unwrap();
        let old = SystemTime::now() - Duration::from_secs(7200);
        for p in [
            target.join("debug/blob"),
            target.join("CACHEDIR.TAG"),
            target.join("debug"),
            target.clone(),
        ] {
            fs::File::open(p).unwrap().set_modified(old).unwrap();
        }
        let home = tmp.path().join("home");
        let units = home.join(".config/systemd/user");
        fs::create_dir_all(&units).unwrap();
        let named = if pin_target_itself { &target } else { &repo };
        fs::write(
            units.join("plangraph-fixture.service"),
            format!(
                "[Service]\nWorkingDirectory={}\nExecStart=/bin/true\n",
                named.display()
            ),
        )
        .unwrap();
        fs::create_dir_all(tmp.path().join("cache/cargo-target")).unwrap();
        let pins = tmp.path().join("pins.json");
        fs::write(&pins, "[]").unwrap();
        let state = tmp.path().join("state");
        fs::create_dir_all(&state).unwrap();
        fs::create_dir_all(tmp.path().join("tmp-sweep")).unwrap();
        let reader = tmp.path().join("process-cwds");
        pg_worker_core::executable_test_support::write_executable(
            &reader,
            "#!/bin/sh\necho '{\"version\":1,\"cwds\":[],\"open_paths\":[],\"cargo_targets\":[]}'\n",
        );
        let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
            .args(["--apply", "--repo"])
            .arg(&repo)
            .arg("--root")
            .arg(&root)
            .arg("--pins")
            .arg(&pins)
            .arg("--tmp-root")
            .arg(tmp.path().join("tmp-sweep"))
            .arg("--state")
            .arg(&state)
            .env("HOME", &home)
            .env_remove("XDG_CONFIG_HOME")
            .env("PG_PROCESS_CWD_READER", &reader)
            .env("PG_PROCESS_INVENTORY", "reader")
            .env(
                "PLANGRAPH_CARGO_TARGET_DIR",
                tmp.path().join("cache/cargo-target"),
            )
            .output()
            .unwrap();
        let row: serde_json::Value =
            serde_json::from_slice(&fs::read(state.join("disk-hygiene.json")).unwrap()).unwrap();
        assert!(output.status.success(), "{row}");
        assert_eq!(
            target.exists(),
            pin_target_itself,
            "pin on the target itself: {pin_target_itself}: {row} {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

/// A shared target configured under a legacy container (`~/.cache/cargo-target/…`) is the
/// host's live target: the legacy sweep must keep it however long it sat idle, while a
/// neighbouring idle legacy target is removed and one holding a git repository is not.
#[test]
fn the_configured_shared_target_under_a_legacy_container_is_never_swept() {
    let (tmp, repo, root) = fixture();
    let home = tmp.path().join("home");
    let container = home.join(".cache/cargo-target");
    let old = SystemTime::now() - Duration::from_secs(3 * 86400);
    let nested = root.join("clean/src/__pycache__");
    let unknown = root.join("clean/target/not-cache");
    pg_worker_core::private_targets::seed_cargo_fixture(&unknown, old, true).unwrap();
    git(&root.join("clean"), &["add", "-f", "target/cache"]);
    git(&root.join("clean"), &["commit", "-qm", "tracked fixture"]);
    fs::write(repo.join(".git/info/exclude"), "__pycache__/\n").unwrap();
    pg_worker_core::private_targets::seed_cargo_fixture(&nested, old, false).unwrap();
    for name in ["shared", "batch23", "holds-repository"] {
        let dir = container.join(name);
        fs::create_dir_all(dir.join("debug")).unwrap();
        fs::write(dir.join(".rustc_info.json"), b"{}").unwrap();
        fs::write(dir.join("debug/blob"), [0u8; 64]).unwrap();
        for p in [
            dir.join("debug/blob"),
            dir.join(".rustc_info.json"),
            dir.join("debug"),
            dir.clone(),
        ] {
            fs::File::open(p).unwrap().set_modified(old).unwrap();
        }
    }
    // An idle legacy target that also holds a bare repository with a commit is source, not cache.
    let mirror = container.join("holds-repository/mirror.git");
    git(&repo, &["init", "-q", "--bare", mirror.to_str().unwrap()]);
    let mut pending = vec![container.join("holds-repository")];
    while let Some(path) = pending.pop() {
        if fs::symlink_metadata(&path).unwrap().is_dir() {
            pending.extend(fs::read_dir(&path).unwrap().map(|e| e.unwrap().path()));
        }
        fs::File::open(&path).unwrap().set_modified(old).unwrap();
    }
    let pins = tmp.path().join("pins.json");
    fs::write(&pins, "[]").unwrap();
    let state = tmp.path().join("state");
    fs::create_dir_all(&state).unwrap();
    fs::create_dir_all(tmp.path().join("tmp-sweep")).unwrap();
    let reader = tmp.path().join("process-cwds");
    pg_worker_core::executable_test_support::write_executable(
        &reader,
        "#!/bin/sh\necho '{\"version\":1,\"cwds\":[],\"open_paths\":[],\"cargo_targets\":[]}'\n",
    );
    let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
        .args(["--apply", "--repo"])
        .arg(&repo)
        .args(["--cleanup-free-bytes", &u64::MAX.to_string()])
        .arg("--root")
        .arg(&root)
        .arg("--pins")
        .arg(&pins)
        .arg("--tmp-root")
        .arg(tmp.path().join("tmp-sweep"))
        .arg("--state")
        .arg(&state)
        .env("HOME", &home)
        .env("XDG_CACHE_HOME", home.join(".cache"))
        .env_remove("PLANGRAPH_WORKTREE_MOUNTS")
        .env_remove("XDG_CONFIG_HOME")
        .env("PG_PROCESS_CWD_READER", &reader)
        .env("PG_PROCESS_INVENTORY", "reader")
        .env("PLANGRAPH_CARGO_TARGET_DIR", container.join("shared"))
        .output()
        .unwrap();
    let row: serde_json::Value =
        serde_json::from_slice(&fs::read(state.join("disk-hygiene.json")).unwrap()).unwrap();
    assert!(output.status.success(), "{row}");
    assert!(container.join("shared/debug/blob").exists(), "{row}");
    assert!(!container.join("batch23").exists(), "{row}");
    assert!(mirror.join("HEAD").exists() && unknown.exists(), "{row}");
    assert!(!nested.exists(), "{row}");
}

/// An aged cargo-marked target that also holds a bare repository with a commit is source: the CLI
/// keeps all of it, while the same aged target without the repository is removed.
#[test]
fn an_aged_target_holding_a_bare_repository_survives_the_cli() {
    for variant in ["bare", "symlinked-refs", "none"] {
        let with_repository = variant != "none";
        let (tmp, repo, root) = fixture();
        age_unrelated_target(&root);
        fs::write(repo.join(".git/info/exclude"), "rust/target/\n").unwrap();
        let target = repo.join("rust/target");
        fs::create_dir_all(target.join("debug")).unwrap();
        fs::write(target.join("debug/blob"), [0u8; 64]).unwrap();
        fs::write(
            target.join("CACHEDIR.TAG"),
            b"Signature: 8a477f597d28d172789f06886806bc55",
        )
        .unwrap();
        let bare = target.join("mirror.git");
        if with_repository {
            git(&repo, &["init", "-q", "--bare", bare.to_str().unwrap()]);
            git(
                &repo,
                &[
                    "--git-dir",
                    bare.to_str().unwrap(),
                    "-c",
                    "user.name=t",
                    "-c",
                    "user.email=t@example.invalid",
                    "commit-tree",
                    "-m",
                    "kept",
                    "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
                ],
            );
        }
        if variant == "symlinked-refs" {
            // Git still reads the commits: refs is a link to a directory outside the target.
            let refs = bare.join("refs");
            let elsewhere = tmp.path().join("shared-refs");
            fs::rename(&refs, &elsewhere).unwrap();
            std::os::unix::fs::symlink(&elsewhere, &refs).unwrap();
        }
        let old = SystemTime::now() - Duration::from_secs(7200);
        let mut pending = vec![target.clone()];
        while let Some(path) = pending.pop() {
            if fs::symlink_metadata(&path).unwrap().is_dir() {
                pending.extend(fs::read_dir(&path).unwrap().map(|e| e.unwrap().path()));
            }
            if !fs::symlink_metadata(&path).unwrap().is_symlink() {
                fs::File::open(&path).unwrap().set_modified(old).unwrap();
            }
        }
        let home = tmp.path().join("home");
        fs::create_dir_all(home.join(".config")).unwrap();
        fs::create_dir_all(tmp.path().join("cache/cargo-target")).unwrap();
        let pins = tmp.path().join("pins.json");
        fs::write(&pins, "[]").unwrap();
        let state = tmp.path().join("state");
        fs::create_dir_all(&state).unwrap();
        fs::create_dir_all(tmp.path().join("tmp-sweep")).unwrap();
        let reader = tmp.path().join("process-cwds");
        pg_worker_core::executable_test_support::write_executable(
            &reader,
            "#!/bin/sh\necho '{\"version\":1,\"cwds\":[],\"open_paths\":[],\"cargo_targets\":[]}'\n",
        );
        let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
            .args(["--apply", "--repo"])
            .arg(&repo)
            .arg("--root")
            .arg(&root)
            .arg("--pins")
            .arg(&pins)
            .arg("--tmp-root")
            .arg(tmp.path().join("tmp-sweep"))
            .arg("--state")
            .arg(&state)
            .env("HOME", &home)
            .env_remove("XDG_CONFIG_HOME")
            .env("PG_PROCESS_CWD_READER", &reader)
            .env("PG_PROCESS_INVENTORY", "reader")
            .env(
                "PLANGRAPH_CARGO_TARGET_DIR",
                tmp.path().join("cache/cargo-target"),
            )
            .output()
            .unwrap();
        let row: serde_json::Value =
            serde_json::from_slice(&fs::read(state.join("disk-hygiene.json")).unwrap()).unwrap();
        assert!(output.status.success(), "{row}");
        assert_eq!(target.exists(), with_repository, "{variant}: {row}");
        if with_repository {
            assert!(bare.join("HEAD").is_file() && target.join("debug/blob").is_file());
        }
    }
}

#[test]
fn final_proof_keeps_its_lease_and_restarts_after_metadata_changes() {
    use pg_worker_core::{
        cargo_cache::try_exclusive_lease,
        disk_hygiene::{Candidate, FinalProof},
    };
    use std::cell::Cell;
    for changed in [false, true] {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().canonicalize().unwrap();
        let now = SystemTime::now();
        let target = root.join("target");
        fs::create_dir(&target).unwrap();
        fs::write(target.join("data-0"), [0u8; 8192]).unwrap();
        for path in [&target, &target.join("data-0")] {
            fs::File::open(path)
                .unwrap()
                .set_modified(now - Duration::from_secs(3600))
                .unwrap();
        }
        let item = Candidate::new(target, root, now).unwrap();
        let target = item.path.clone();
        let file = target.join("data-0");
        let mut proof = FinalProof::begin(item).unwrap();
        let steps = Cell::new(2usize);
        assert!(!proof
            .advance(|| {
                let previous = steps.get();
                steps.set(previous.saturating_sub(1));
                previous > 0
            })
            .unwrap());
        assert!(try_exclusive_lease(&target).unwrap().is_none());
        assert!(proof.remove(true, || Ok(true), || true).is_err());
        if changed {
            fs::File::open(&file).unwrap().set_modified(now).unwrap();
            assert_eq!(proof.advance(|| true), Err("young"));
            assert!(target.exists());
        } else {
            assert!(proof.advance(|| true).unwrap());
            assert_eq!(proof.remove(true, || Ok(false), || true).unwrap(), None);
            assert!(target.exists());
            assert!(proof.remove(true, || Ok(true), || true).unwrap().unwrap() > 0);
            assert!(!target.exists());
        }
    }
}
