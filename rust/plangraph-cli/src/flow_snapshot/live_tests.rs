//! The live readers against fixtures: a temp git origin, unit directories and a series store.
use super::*;
use std::{fs, process::Command};

fn git_in(dir: &Path, args: &[&str]) {
    let status = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
        .args(args)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .status()
        .unwrap();
    assert!(status.success(), "git {args:?}");
}

fn commit(dir: &Path, name: &str) {
    fs::write(dir.join(name), name).unwrap();
    git_in(dir, &["add", "."]);
    git_in(dir, &["commit", "-q", "-m", name]);
}

#[test]
fn the_default_series_store_is_the_samplers_fleet_directory() {
    let state = tempfile::tempdir().unwrap();
    let env = BTreeMap::from([(
        "PLANGRAPH_STATE_DIR".into(),
        state.path().display().to_string(),
    )]);
    assert_eq!(series_dir(&env).unwrap(), state.path().join("fleet"));
    let mut sample = pg_worker_core::fleet_series::Sample {
        host: "vm".into(),
        ts: 100,
        runs: 2,
        ready: 1,
        reachable: true,
        ..Default::default()
    };
    pg_worker_core::fleet_series::append(&state.path().join("fleet"), &mut sample).unwrap();
    let rows = series(&series_dir(&env).unwrap()).unwrap();
    assert_eq!(rows[0]["host"], "vm");
    assert_eq!(
        (rows[0]["runs"].clone(), rows[0]["ready"].clone()),
        (json!(2), json!(1))
    );
}

#[test]
fn host_code_fetches_origin_before_it_counts_what_is_missing() {
    let dir = tempfile::tempdir().unwrap();
    let origin = dir.path().join("origin");
    let work = dir.path().join("work");
    fs::create_dir(&origin).unwrap();
    git_in(&origin, &["init", "-q", "-b", "main"]);
    commit(&origin, "a");
    git_in(
        dir.path(),
        &[
            "clone",
            "-q",
            origin.to_str().unwrap(),
            work.to_str().unwrap(),
        ],
    );
    commit(&origin, "b");
    commit(&origin, "c");
    // The clone's origin/main still names the first commit until the reader fetches.
    let row = host_code("box", &work).unwrap();
    assert_eq!(row[0]["behind_commits"], 2);
    assert_ne!(row[0]["checkout_head"], row[0]["origin_main_head"]);
    assert_eq!(row[0]["host"], "box");
    assert!(row[0]["age_hours"].as_f64().unwrap() >= 0.0);
    fs::remove_dir_all(&origin).unwrap();
    assert!(
        host_code("box", &work).is_err(),
        "a failed fetch must be a gap"
    );
}

#[test]
fn units_compare_installed_files_and_skip_templates_and_missing_ones() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("root");
    let installed = dir.path().join("installed");
    fs::create_dir_all(root.join("deploy/systemd")).unwrap();
    fs::create_dir_all(root.join("deploy/watchdog")).unwrap();
    fs::create_dir(&installed).unwrap();
    for (name, source) in [
        ("same.service", "[Unit]\nA\n"),
        ("changed.timer", "[Timer]\nnew\n"),
        ("absent.service", "[Unit]\nB\n"),
        ("templated.service", "ExecStart=@ROOT@/x\n"),
    ] {
        fs::write(root.join("deploy/systemd").join(name), source).unwrap();
    }
    fs::write(installed.join("same.service"), "[Unit]\nA\n").unwrap();
    fs::write(installed.join("changed.timer"), "[Timer]\nold\n").unwrap();
    fs::write(installed.join("templated.service"), "ExecStart=/r/x\n").unwrap();
    let rows = units("box", &root, &installed).unwrap();
    let rows = rows.as_array().unwrap();
    let mut names: Vec<_> = rows.iter().map(|r| r["name"].as_str().unwrap()).collect();
    names.sort();
    assert_eq!(names, ["changed.timer", "same.service"]);
    for row in rows {
        let same = row["deployed_sha"] == row["expected_sha"];
        assert_eq!(same, row["name"] == "same.service", "{row}");
    }
}
