use super::{fs, install, Command, Path};

#[test]
fn disk_hygiene_environment_matches_unit_and_preserves_existing_configuration() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().join("fixture home");
    let config = home.join(".config/plangraph");
    let env = config.join("disk-hygiene-service.env");
    let shared = config.join("disk-hygiene.env");
    let output = install(&home, "disk-hygiene");
    assert!(output.status.success(), "{output:?}");
    assert!(
        !shared.exists(),
        "disk installer preempted the sweep environment"
    );
    let text = fs::read_to_string(&env).unwrap();
    for (key, path) in [
        ("PG_REPO", "code/plangraph"),
        ("PG_WORKTREES", "code/plangraph/.claude/worktrees"),
        ("PG_PINS", ".config/plangraph/pins.json"),
    ] {
        assert!(
            text.contains(&format!("{key}=\"{}\"\n", home.join(path).display())),
            "{text}"
        );
    }
    let binary = home.join(".local/bin/pg-disk-hygiene");
    fs::create_dir_all(binary.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink("/bin/true", binary).unwrap();
    let unit = include_str!("../../../../deploy/systemd/pg-disk-hygiene.service");
    let env_paths: Vec<_> = unit
        .lines()
        .filter_map(|line| line.strip_prefix("EnvironmentFile="))
        .collect();
    assert_eq!(
        env_paths,
        [
            "-%h/.config/plangraph/disk-hygiene.env",
            "-%h/.config/plangraph/disk-hygiene-service.env",
        ]
    );
    if Path::new("/usr/bin/systemd-analyze").is_file() {
        let service = temp.path().join("pg-disk-hygiene.service");
        let unit = unit.replace(
            "%h/.local/bin/pg-disk-hygiene",
            &format!("\"{}\"", home.join(".local/bin/pg-disk-hygiene").display()),
        );
        fs::write(&service, unit).unwrap();
        let verify = Command::new("/usr/bin/systemd-analyze")
            .arg("verify")
            .arg(service)
            .output()
            .unwrap();
        assert!(verify.status.success(), "{verify:?}");
    }
    fs::write(&env, "PG_REPO=existing\n").unwrap();
    assert!(install(&home, "disk-hygiene").status.success());
    assert_eq!(fs::read_to_string(&env).unwrap(), "PG_REPO=existing\n");
    let token = config.join("sweep.token");
    fs::write(&token, "fixture").unwrap();
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let sweep = Command::new("python3")
        .env("HOME", &home)
        .env("PG_URL", "https://fixture.invalid")
        .env("PYTHONPATH", repo.join("src"))
        .args([
            "-m",
            "plangraph.keeper.cli",
            "admin",
            "install-sweep",
            "--example",
        ])
        .arg(repo.join("deploy/systemd/disk-hygiene.env.example"))
        .arg("--config-dir")
        .arg(&config)
        .arg("--state-dir")
        .arg(home.join(".local/state/plangraph"))
        .arg("--token-file")
        .arg(&token)
        .arg("--env-file")
        .arg(&shared)
        .arg("--pins-file")
        .arg(config.join("pins.json"))
        .arg("--repo-root")
        .arg(&repo)
        .output()
        .unwrap();
    assert!(sweep.status.success(), "{sweep:?}");
    let sweep_env = fs::read_to_string(&shared).unwrap();
    let sweep_unit = include_str!("../../../../deploy/systemd/pg-worktree-sweep.service");
    assert!(sweep_unit.contains("EnvironmentFile=%h/.config/plangraph/disk-hygiene.env"));
    let exec = sweep_unit
        .lines()
        .find_map(|line| line.strip_prefix("ExecStart="))
        .unwrap();
    let keys = exec.split_whitespace().filter_map(|arg| {
        arg.strip_prefix("${")
            .and_then(|value| value.strip_suffix('}'))
    });
    for key in keys.chain(["PG_SWEEP_CLAIMS_READER", "PG_URL", "PG_TOKEN_FILE"]) {
        let value = sweep_env
            .lines()
            .find_map(|line| line.strip_prefix(&format!("{key}=")));
        assert!(
            value.is_some_and(|value| !value.is_empty()),
            "missing {key}"
        );
    }
    fs::remove_file(&env).unwrap();
    assert!(install(&home, "disk-hygiene").status.success());
    assert!(
        !env.exists(),
        "installer overrode a usable legacy environment"
    );
    assert_eq!(fs::read_to_string(shared).unwrap(), sweep_env);
}
