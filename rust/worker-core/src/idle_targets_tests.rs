use super::*;
use std::fs;
use std::path::Path;

fn target(container: &Path, name: &str, age: Duration) -> PathBuf {
    let path = container.join(name);
    fs::create_dir_all(path.join("debug")).unwrap();
    fs::write(path.join(".rustc_info.json"), b"{}").unwrap();
    fs::write(path.join("debug/blob"), [0u8; 64]).unwrap();
    let old = SystemTime::now() - age;
    for p in [
        path.join("debug/blob"),
        path.join(".rustc_info.json"),
        path.join("debug"),
        path.clone(),
    ] {
        fs::File::open(p).unwrap().set_modified(old).unwrap();
    }
    path
}

fn run(containers: &[PathBuf], guards: &Guards, apply: bool, building: bool) -> Report {
    let mut report = Report::default();
    sweep(
        containers,
        guards,
        SystemTime::now(),
        apply,
        &mut report,
        || Ok((vec![], building)),
    )
    .unwrap();
    report
}

#[test]
fn only_targets_idle_for_a_day_are_removed_and_the_container_stays() {
    let tmp = tempfile::tempdir().unwrap();
    let container = tmp.path().join("cargo-target");
    let old = target(&container, "batch23", Duration::from_secs(3 * 86400));
    let recent = target(&container, "shared", Duration::from_secs(3600));
    fs::create_dir_all(container.join("notes")).unwrap();
    let dry = run(
        std::slice::from_ref(&container),
        &Guards::default(),
        false,
        false,
    );
    assert!(dry.actions.iter().any(|a| a.starts_with("would-remove")));
    assert!(old.exists());
    let applied = run(
        std::slice::from_ref(&container),
        &Guards::default(),
        true,
        false,
    );
    assert!(applied.freed_bytes > 0);
    assert!(!old.exists() && recent.exists() && container.join("notes").exists());
}

#[test]
fn a_running_build_a_pinned_or_a_plain_directory_keeps_its_target() {
    let tmp = tempfile::tempdir().unwrap();
    let container = tmp.path().join("cargo-target");
    let old = target(&container, "batch24", Duration::from_secs(3 * 86400));
    assert_eq!(
        run(
            std::slice::from_ref(&container),
            &Guards::default(),
            true,
            true
        )
        .freed_bytes,
        0
    );
    assert!(old.exists());
    let pinned = Guards {
        pinned: vec![old.canonicalize().unwrap()],
        live: vec![],
    };
    assert_eq!(
        run(std::slice::from_ref(&container), &pinned, true, false).freed_bytes,
        0
    );
    assert!(old.exists());
    // Without cargo's markers a directory is not a target, whatever it is named or how old it is.
    let plain = container.join("project");
    fs::create_dir_all(plain.join("debug")).unwrap();
    let ancient = SystemTime::now() - Duration::from_secs(30 * 86400);
    fs::File::open(plain.join("debug"))
        .unwrap()
        .set_modified(ancient)
        .unwrap();
    fs::File::open(&plain)
        .unwrap()
        .set_modified(ancient)
        .unwrap();
    run(&[container], &Guards::default(), true, false);
    assert!(plain.join("debug").exists());
}

#[test]
fn a_container_that_is_itself_a_target_and_a_missing_one_are_handled() {
    let tmp = tempfile::tempdir().unwrap();
    let whole = tmp.path().join("pg-target-warm");
    fs::create_dir_all(&whole).unwrap();
    let inner = target(tmp.path(), "pg-target-warm", Duration::from_secs(5 * 86400));
    assert_eq!(inner, whole);
    let missing = tmp.path().join("absent");
    let report = run(&[missing, whole.clone()], &Guards::default(), true, false);
    assert!(report.freed_bytes > 0 && !whole.exists());
}
