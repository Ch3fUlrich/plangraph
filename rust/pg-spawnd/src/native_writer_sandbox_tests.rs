use super::*;

fn roots() -> Vec<PathBuf> {
    writable_roots(
        Path::new("/cache/target"),
        Path::new("/run/reviews"),
        Path::new("/run/dir"),
        &[
            PathBuf::from("/repo/.git"),
            PathBuf::from("/repo/.git/worktrees/w"),
        ],
    )
}

#[test]
fn command_line_is_workspace_write_with_network_and_named_roots() {
    let deny = [PathBuf::from("/fixture/user/.config/plangraph")];
    let args = sandbox_args(&roots(), &deny).unwrap();
    assert_eq!(
        args[..6],
        [
            "-c",
            "sandbox_mode=\"workspace-write\"",
            "-c",
            "sandbox_workspace_write.network_access=true",
            "-c",
            "sandbox_workspace_write.writable_roots=[\"/cache/target\",\"/run/reviews\",\"/run/dir\",\"/repo/.git\",\"/repo/.git/worktrees/w\"]",
        ]
    );
    let profile = args[6..].join(" ");
    assert!(
        profile.contains("default_permissions=\"plangraph_isolated\""),
        "{profile}"
    );
    assert!(
        profile.contains("\"/fixture/user/.config/plangraph\"=\"none\""),
        "{profile}"
    );
    assert!(profile.contains("\"/cache/target\"=\"write\""), "{profile}");
    assert!(!args.join(" ").contains("danger-full-access"));
}

#[test]
fn duplicate_roots_are_named_once() {
    let same = Path::new("/same");
    assert_eq!(
        writable_roots(same, same, same, &[same.to_path_buf()]),
        [PathBuf::from("/same")]
    );
}

#[test]
fn only_allowlisted_variables_survive() {
    let var = |n: &str| (OsString::from(n), OsString::from("x"));
    let kept = allowed_env([
        var("HOME"),
        var("CODEX_HOME"),
        var("LC_ALL"),
        var("HTTPS_PROXY"),
        var("LC_FIXTURE_PASSWORD"),
        var("AWS_SECRET_ACCESS_KEY"),
        var("GITHUB_TOKEN"),
        var("OMNIROUTE_API_KEY"),
        var("PATH"),
    ]);
    let names: Vec<_> = kept.iter().map(|(n, _)| n.to_str().unwrap()).collect();
    assert_eq!(names, ["HOME", "CODEX_HOME", "LC_ALL", "HTTPS_PROXY"]);
}

#[cfg(unix)]
#[test]
fn a_variable_outside_the_allowlist_does_not_reach_the_child_process() {
    let mut command = Command::new("env");
    apply_env(
        &mut command,
        [
            (OsString::from("HOME"), OsString::from("/fixture/user")),
            (OsString::from("FLEET_SECRET_TOKEN"), OsString::from("leak")),
        ],
        "/usr/bin:/bin",
    );
    let out = command.output().unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(text.lines().any(|l| l == "HOME=/fixture/user"), "{text}");
    assert!(text.lines().any(|l| l == "PATH=/usr/bin:/bin"), "{text}");
    assert!(
        !text.contains("FLEET_SECRET_TOKEN") && !text.contains("leak"),
        "{text}"
    );
}

/// Real Codex sandbox: a write inside a named root works, one outside every root is refused.
#[cfg(target_os = "linux")]
#[test]
#[ignore = "live host check requiring a working Codex sandbox"]
fn a_write_outside_the_named_roots_is_refused_in_a_fixture_run() {
    // Outside /tmp and $TMPDIR, which the mode keeps writable for builds.
    let dir = tempfile::tempdir_in(env!("CARGO_MANIFEST_DIR")).unwrap();
    let (workspace, named, outside) = (
        dir.path().join("workspace"),
        dir.path().join("named"),
        dir.path().join("outside"),
    );
    for d in [&workspace, &named, &outside] {
        std::fs::create_dir(d).unwrap();
    }
    let script = "touch \"$1/in\" && echo named-ok; touch in && echo workspace-ok; touch \"$2/out\" || echo outside-refused";
    let out = Command::new("codex")
        .arg("sandbox")
        .args(sandbox_args(std::slice::from_ref(&named), &[]).unwrap())
        .args(["--", "sh", "-c", script, "fixture"])
        .arg(&named)
        .arg(&outside)
        .current_dir(&workspace)
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success() && text.contains("named-ok") && text.contains("workspace-ok"),
        "{text}{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        text.contains("outside-refused") && !outside.join("out").exists(),
        "{text}"
    );
}

/// A worktree's commits need its own git directory named: the real sandbox keeps it read-only
/// otherwise, which would stop the writer at its first commit.
#[cfg(target_os = "linux")]
#[test]
#[ignore = "live host check requiring a working Codex sandbox"]
fn a_writer_worktree_can_commit_with_the_named_git_directories() {
    let dir = tempfile::tempdir_in(env!("CARGO_MANIFEST_DIR")).unwrap();
    let dir = std::fs::canonicalize(dir.path()).map(|p| (p, dir)).unwrap();
    let (root, _guard) = dir;
    let git = |args: &[&str], cwd: &Path| {
        let out = Command::new("git")
            .arg("-C")
            .arg(cwd)
            .args(args)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    };
    let ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"];
    git(&["init", "-q", "repo"], &root);
    git(
        &[
            ident[0],
            ident[1],
            ident[2],
            ident[3],
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        &root.join("repo"),
    );
    git(
        &["worktree", "add", "-q", "--detach", "../wt"],
        &root.join("repo"),
    );
    let wt = root.join("wt");
    let path = std::env::var("PATH").unwrap();
    let dirs = git_dirs(&wt, Some(&path)).unwrap();
    assert_eq!(dirs.len(), 2, "{dirs:?}");
    let roots = writable_roots(
        &root.join("target"),
        &root.join("reviews"),
        &root.join("run"),
        &dirs,
    );
    let script = "echo x > f && git add f && git -c user.name=t -c user.email=t@example.invalid commit -qm c && echo commit-ok";
    let out = Command::new("codex")
        .arg("sandbox")
        .args(sandbox_args(&roots, &[]).unwrap())
        .args(["--", "sh", "-c", script])
        .current_dir(&wt)
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success() && text.contains("commit-ok"),
        "{text}{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
