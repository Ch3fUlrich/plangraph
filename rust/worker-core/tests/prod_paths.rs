use pg_worker_core::prod_paths::{self, Reference};
use std::{fs, path::PathBuf};

#[test]
#[cfg(unix)]
fn prod_paths_cli_reads_only_fixture_hooks_and_crontab() {
    use std::{os::unix::fs::PermissionsExt, process::Command};
    let tmp = tempfile::tempdir().unwrap();
    let home = tmp.path();
    let bin = home.join("bin");
    let repo = home.join("code/plangraph");
    let units = home.join("units");
    for p in [&bin, &repo, &units] {
        fs::create_dir_all(p).unwrap();
    }
    fs::write(
        bin.join("crontab"),
        "#!/bin/sh\nprintf '%s\\n' '* * * * * /usr/bin/true /code/routing/job'\n",
    )
    .unwrap();
    fs::set_permissions(bin.join("crontab"), fs::Permissions::from_mode(0o755)).unwrap();
    pg_worker_core::prod_paths::command_output(
        Command::new("git")
            .arg("-C")
            .arg(&repo)
            .args(["init", "-q"]),
    )
    .unwrap();
    // Config writes are confined to the fixture files, never the real user's config.
    fs::write(
        repo.join(".git/config"),
        "[core]\n hooksPath = /usr/plangraph-hooks\n",
    )
    .unwrap();
    fs::write(
        home.join(".gitconfig"),
        "[core]\n hooksPath = ~/old-worktrees/hooks\n",
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
        .args(["prod-paths", "--json", "--unit-dir"])
        .arg(&units)
        .arg("--repo")
        .arg(&repo)
        .env("HOME", home)
        .env("XDG_CONFIG_HOME", home.join("xdg"))
        .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
        .env("GIT_DIR", "/nonexistent/poison")
        .env("GIT_CONFIG_COUNT", "1")
        .env("GIT_CONFIG_KEY_0", "core.hooksPath")
        .env("GIT_CONFIG_VALUE_0", "/usr/poison")
        .output()
        .unwrap();
    assert_eq!(
        output.status.code(),
        Some(1),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let value: serde_json::Value = serde_json::from_slice(&output.stdout)
        .unwrap_or_else(|e| panic!("{e}: stderr={}", String::from_utf8_lossy(&output.stderr)));
    assert_eq!(value["issues"].as_array().unwrap().len(), 3);
    assert!(!String::from_utf8_lossy(&output.stdout).contains("poison"));
}

#[test]
fn prod_paths_units_dropins_environment_and_hooks() {
    let tmp = tempfile::tempdir().unwrap();
    let home = tmp.path();
    let dir = home.join("units");
    fs::create_dir_all(dir.join("worker.service.d")).unwrap();
    fs::write(dir.join("clean.service"), "[Service]\nExecStart=/usr/bin/true\nWorkingDirectory=%h/code/plangraph\nEnvironment=\"PYTHONPATH=%h/code/plangraph\"\n").unwrap();
    fs::write(
        dir.join("worker.service"),
        "[Service]\nExecStart=%h/code/worktrees/old/run\n",
    )
    .unwrap();
    fs::write(dir.join("worker.service.d/override.conf"), "[Service]\nExecStart=\nExecStart=%h/code/plangraph/run\nExecStartPre=%h/.claude/worktrees/pre/run\n").unwrap();
    let refs = prod_paths::unit_references(&dir, home).unwrap();
    assert!(!refs.iter().any(|r| r.path.ends_with("old/run")));
    let issues = prod_paths::issues(&refs, &prod_paths::default_roots(home));
    assert_eq!(issues.len(), 1);
    assert_eq!(issues[0].kind, "ExecStartPre");
    assert!(issues[0].file.ends_with("override.conf"));
    fs::write(
        dir.join("bad.service"),
        "[Service]\nExecStart=%h/repo-worktrees/task/run\n",
    )
    .unwrap();
    assert_eq!(
        prod_paths::issues(
            &prod_paths::unit_references(&dir, home).unwrap(),
            &prod_paths::default_roots(home)
        )
        .len(),
        2
    );
    let hook = Reference {
        kind: "hooksPath".into(),
        file: home.join(".gitconfig"),
        path: home.join("plangraph-hooks"),
    };
    assert_eq!(prod_paths::issues(&[hook], &[home.into()]).len(), 1);
}

#[test]
fn prod_paths_envfiles_roots_and_normalization() {
    let tmp = tempfile::tempdir().unwrap();
    fs::write(
        tmp.path().join("env"),
        "PYTHONPATH=/code/routing/lib:/opt/app\n",
    )
    .unwrap();
    fs::write(
        tmp.path().join("worker.service"),
        format!(
            "[Service]\nEnvironmentFile={}\n",
            tmp.path().join("env").display()
        ),
    )
    .unwrap();
    let refs = prod_paths::unit_references(tmp.path(), tmp.path()).unwrap();
    assert!(refs
        .iter()
        .any(|r| r.path == PathBuf::from("/code/routing/lib")));
    assert_eq!(
        prod_paths::issues(&refs, &[tmp.path().into(), "/opt".into()]).len(),
        1
    );
    assert!(prod_paths::forbidden(&prod_paths::normalize(
        &PathBuf::from("/usr/../AutoOS/run"),
        tmp.path()
    )));
}
