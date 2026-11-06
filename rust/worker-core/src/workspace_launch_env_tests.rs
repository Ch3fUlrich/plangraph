use super::{
    tests::{launch, Fixture},
    *,
};

#[test]
#[cfg(unix)]
fn none_mode_child_runs_in_temp_not_process_cwd() {
    let mut f = Fixture::new();
    let tree = f.writer();
    f.ws.mode = Mode::None;
    f.ws.base_sha = None;
    f.ws.revision_sha = None;
    f.ws.branch = None;
    let temp = f.tmp.path().join("temp-link");
    std::os::unix::fs::symlink(f.tmp.path(), &temp).unwrap();
    let mut cmd = Command::new("/bin/sh");
    cmd.args([
        "-c",
        "pwd -P; printf '%s\\n' \"$PWD\"; test -z \"$GIT_DIR$KEEPER_DATABASE_TOKEN\"",
    ]);
    crate::sandbox_env::confine_check(&mut cmd, f.tmp.path());
    cmd.env("GIT_DIR", "wrong")
        .env("KEEPER_DATABASE_TOKEN", "wrong")
        .env("PWD", "wrong");
    assert!(
        prepare_launch(&mut cmd, &f.repo, &f.ws, None, "seat", &temp, None)
            .unwrap()
            .is_none()
    );
    assert!(!cmd.get_envs().any(|(k, _)| k == "PYTHONPATH"));
    let out = cmd.output().unwrap();
    assert!(out.status.success());
    let expected = f.tmp.path().canonicalize().unwrap();
    assert_ne!(expected, std::env::current_dir().unwrap());
    let stdout = String::from_utf8(out.stdout).unwrap();
    assert_eq!(
        stdout.lines().collect::<Vec<_>>(),
        vec![expected.to_str().unwrap(), temp.to_str().unwrap()]
    );
    assert!(launch(&f, &mut cmd, Some(&tree)).is_err());
}
#[test]
fn checkout_mode_applies_env_before_canary() {
    let f = Fixture::new();
    let tree = f.writer();
    verify_workspace(&f.repo, &f.ws, &tree, None).unwrap();
    git(&tree, &["commit", "--allow-empty", "-m", "changed"]).unwrap();
    let mut cmd = Command::new("unused");
    cmd.env("GIT_DIR", "wrong")
        .env("KEEPER_DATABASE_TOKEN", "wrong");
    let result = launch(&f, &mut cmd, Some(&tree));
    assert!(result.is_err());
    assert_eq!(cmd.get_current_dir(), Some(tree.as_path()));
    let pwd = cmd.get_envs().find(|(k, _)| *k == "PWD").unwrap().1;
    assert_eq!(pwd, Some(tree.as_os_str()));
    assert!(!cmd.get_envs().any(|(k, v)| k == "GIT_DIR" && v.is_some()));
    assert!(!cmd
        .get_envs()
        .any(|(k, v)| k == "KEEPER_DATABASE_TOKEN" && v.is_some()));
    git(
        &tree,
        &["reset", "--hard", f.ws.base_sha.as_deref().unwrap()],
    )
    .unwrap();
    fs::write(tree.join("tracked"), "dirty").unwrap();
    assert!(matches!(
        launch(&f, &mut cmd, Some(&tree)),
        Err(ResolveError::Verify("dirty"))
    ));
    assert!(launch(&f, &mut Command::new("unused"), None).is_err());
}
