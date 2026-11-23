#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use std::{
    fs,
    path::Path,
    process::{Command, Output},
};
fn git(repo: &Path, args: &[&str]) -> String {
    let mut cmd = pg_worker_core::git_test_support::command(repo);
    let out = cmd
        .current_dir(repo)
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .env("GIT_COMMITTER_DATE", "2026-01-01T00:00:00Z")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().into()
}
fn fixture() -> tempfile::TempDir {
    let tmp = tempfile::tempdir().unwrap();
    #[cfg(unix)]
    {
        fs::create_dir(tmp.path().join("bin")).unwrap();
        pg_worker_core::executable_test_support::write_executable(
            tmp.path().join("bin/systemctl"),
            "#!/bin/sh\nexit 0\n",
        );
    }
    let seed = tmp.path().join("source");
    fs::create_dir(&seed).unwrap();
    git(&seed, &["init", "-qb", "main"]);
    fs::write(seed.join("file"), "one").unwrap();
    git(&seed, &["add", "."]);
    git(&seed, &["commit", "-qm", "one"]);
    git(tmp.path(), &["clone", "-q", "source", "checkout"]);
    fs::write(seed.join("file"), "two").unwrap();
    git(&seed, &["commit", "-qam", "two"]);
    tmp
}
fn run(cwd: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"))
        .current_dir(cwd)
        .env(
            "PATH",
            format!(
                "{}:{}",
                cwd.join("bin").display(),
                std::env::var("PATH").unwrap()
            ),
        )
        .args(args)
        .output()
        .unwrap()
}
#[test]
fn all_five_reasons_and_explicit_label() {
    for reason in [
        "detached",
        "other-branch:topic",
        "tracked-changes",
        "diverged",
        "fetch-failed",
    ] {
        let tmp = fixture();
        let repo = tmp.path().join("checkout");
        match reason {
            "detached" => {
                git(&repo, &["checkout", "-q", "--detach"]);
            }
            "other-branch:topic" => {
                git(&repo, &["checkout", "-qb", "topic"]);
            }
            "tracked-changes" => {
                fs::write(repo.join("file"), "dirty").unwrap();
            }
            "diverged" => {
                fs::write(repo.join("local"), "local").unwrap();
                git(&repo, &["add", "."]);
                git(&repo, &["commit", "-qm", "local"]);
            }
            _ => {
                git(&repo, &["remote", "set-url", "origin", "missing-origin"]);
            }
        }
        let out = run(tmp.path(), &["--repo", "checkout"]);
        assert_eq!(out.status.code(), Some(1));
        let text = String::from_utf8(out.stdout).unwrap();
        assert_eq!(text.lines().count(), 1);
        assert!(text.starts_with("ALERT checkout "), "{text}");
        assert!(text.ends_with(&format!("reason={reason}\n")), "{text}");
        assert!(!text.contains("behind=0"));
    }
}
#[test]
fn fast_forward_and_missing_home_or_code_are_silent() {
    let tmp = fixture();
    let out = run(tmp.path(), &["--repo", "checkout"]);
    assert!(out.status.success() && out.stdout.is_empty() && out.stderr.is_empty());
    assert_eq!(
        git(&tmp.path().join("checkout"), &["rev-parse", "HEAD"]),
        git(&tmp.path().join("source"), &["rev-parse", "HEAD"])
    );
    for home in [None, Some(tmp.path())] {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"));
        cmd.current_dir(tmp.path()).env_remove("HOME");
        if let Some(home) = home {
            cmd.env("HOME", home);
        }
        let out = cmd.output().unwrap();
        assert!(out.status.success() && out.stdout.is_empty() && out.stderr.is_empty());
    }
}
#[test]
fn direct_children_and_symlink_children_are_discovered() {
    let tmp = fixture();
    let root = tmp.path().join("code");
    fs::create_dir(&root).unwrap();
    fs::rename(tmp.path().join("checkout"), root.join("checkout")).unwrap();
    let repo = root.join("checkout");
    git(
        &repo,
        &[
            "remote",
            "set-url",
            "origin",
            tmp.path().join("source").to_str().unwrap(),
        ],
    );
    fs::write(repo.join("file"), "dirty").unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&repo, root.join("linked")).unwrap();
    let out = run(tmp.path(), &["--root", root.to_str().unwrap()]);
    assert_eq!(out.status.code(), Some(1));
    let text = String::from_utf8(out.stdout).unwrap();
    assert_eq!(text.lines().count(), if cfg!(unix) { 2 } else { 1 });
    assert!(text.contains("checkout") && text.contains("reason=tracked-changes"));
    #[cfg(unix)]
    assert!(text.contains("linked"));
}

#[test]
fn nested_path_never_fetches_or_reads_parent_config() {
    let tmp = fixture();
    let repo = tmp.path().join("checkout");
    let before = git(&repo, &["rev-parse", "HEAD"]);
    fs::create_dir(repo.join("nested")).unwrap();
    let out = run(tmp.path(), &["--repo", "checkout/nested"]);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(git(&repo, &["rev-parse", "HEAD"]), before);
    assert_ne!(
        git(&repo, &["rev-parse", "origin/main"]),
        git(&tmp.path().join("source"), &["rev-parse", "HEAD"])
    );
    let out = run(
        tmp.path(),
        &[
            "--repo",
            "checkout/nested",
            "--check-config",
            "--host",
            "test",
            "--allow-name",
            "Fixture",
            "--allow-email",
            "test@example.invalid",
        ],
    );
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8(out.stdout)
        .unwrap()
        .contains("reason=fetch-failed"));
}
#[cfg(unix)]
#[test]
fn bounded_status_continues_to_next_repository() {
    let tmp = fixture();
    let bin = tmp.path().join("bin");
    fs::create_dir_all(&bin).unwrap();
    let real = Command::new("which").arg("git").output().unwrap();
    let real = String::from_utf8(real.stdout).unwrap();
    pg_worker_core::executable_test_support::write_executable(
        bin.join("git"),
        format!(
            "#!/bin/sh\ncase \"$*\" in *checkout*status*) sleep 3;; esac\nexec {} \"$@\"\n",
            real.trim()
        ),
    );
    git(tmp.path(), &["clone", "-q", "source", "next"]);
    let start = std::time::Instant::now();
    let out = Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"))
        .current_dir(tmp.path())
        .args(["--repo", "checkout", "--repo", "next"])
        .env(
            "PATH",
            format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()),
        )
        .env("PG_REPO_SYNC_TIMEOUT_SECS", "1")
        .output()
        .unwrap();
    assert!(start.elapsed() < std::time::Duration::from_millis(2500));
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8(out.stdout)
        .unwrap()
        .contains("reason=tracked-changes"));
    assert_eq!(
        git(&tmp.path().join("next"), &["rev-parse", "HEAD"]),
        git(&tmp.path().join("source"), &["rev-parse", "HEAD"])
    );
}
#[cfg(unix)]
#[test]
fn live_unit_checkout_is_reported_and_unchanged() {
    let tmp = fixture();
    let bin = tmp.path().join("bin");
    fs::create_dir_all(&bin).unwrap();
    pg_worker_core::executable_test_support::write_executable(bin.join("systemctl"), format!("#!/bin/sh\ncase \"$*\" in *list-units*) echo 'keeper.service loaded active running Keeper';; *show*) echo '{}';; esac\n", tmp.path().join("checkout").display()));
    let before = git(&tmp.path().join("checkout"), &["rev-parse", "HEAD"]);
    let out = Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"))
        .current_dir(tmp.path())
        .args(["--repo", "checkout"])
        .env(
            "PATH",
            format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()),
        )
        .output()
        .unwrap();
    assert!(String::from_utf8(out.stdout).unwrap().contains("live-unit"));
    assert_eq!(
        git(&tmp.path().join("checkout"), &["rev-parse", "HEAD"]),
        before
    );
}
#[test]
fn excluded_alias_or_pinned_checkout_is_reported_without_fetch() {
    for pin in [false, true] {
        let tmp = fixture();
        let repo = tmp.path().join("checkout");
        let before = git(&repo, &["rev-parse", "origin/main"]);
        let mut args = vec!["--repo", "checkout"];
        if pin {
            fs::write(repo.join(".repo-sync-pin"), "operator pin").unwrap();
        } else {
            args.extend(["--exclude", "./checkout"]);
        }
        let out = run(tmp.path(), &args);
        assert!(out.status.success());
        assert!(String::from_utf8(out.stdout).unwrap().contains("SKIP"));
        assert_eq!(git(&repo, &["rev-parse", "origin/main"]), before);
        assert_eq!(git(&repo, &["rev-parse", "HEAD"]), before);
    }
}
#[cfg(unix)]
#[test]
fn systemd_optional_and_home_directories_do_not_disable_sync() {
    for value in ["!", "~", "!/no-such-pg-repo-sync-directory"] {
        let tmp = fixture();
        let value = if value == "!" {
            format!("!{}", tmp.path().display())
        } else {
            value.into()
        };
        pg_worker_core::executable_test_support::write_executable(tmp.path().join("bin/systemctl"), format!("#!/bin/sh\ncase \"$*\" in --user*list-units*) echo 'other.service loaded active running Other';; *show*) echo '{}';; esac\n", value));
        let out = Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"))
            .current_dir(tmp.path())
            .args(["--repo", "checkout"])
            .env("HOME", tmp.path())
            .env(
                "PATH",
                format!(
                    "{}:{}",
                    tmp.path().join("bin").display(),
                    std::env::var("PATH").unwrap()
                ),
            )
            .output()
            .unwrap();
        assert!(out.status.success() && out.stdout.is_empty(), "{:?}", out);
        assert_eq!(
            git(&tmp.path().join("checkout"), &["rev-parse", "HEAD"]),
            git(&tmp.path().join("source"), &["rev-parse", "HEAD"])
        );
    }
}
#[cfg(unix)]
#[test]
fn unreadable_unit_protection_fails_closed_with_nonzero_exit() {
    for script in ["exit 1", "case \"$*\" in *list-units*) echo 'bad.service loaded active running Bad';; *show*) echo '/no-such-pg-repo-sync-directory';; esac"] {
        let tmp = fixture();
        fs::write(tmp.path().join("bin/systemctl"), format!("#!/bin/sh\n{script}\n")).unwrap();
        let before = git(&tmp.path().join("checkout"), &["rev-parse", "origin/main"]);
        let out = run(tmp.path(), &["--repo", "checkout"]);
        assert_eq!(out.status.code(), Some(1));
        assert!(String::from_utf8(out.stdout).unwrap().contains("SKIP"));
        assert_eq!(git(&tmp.path().join("checkout"), &["rev-parse", "origin/main"]), before);
    }
}

#[test]
fn external_hook_checkout_is_fast_forwarded_or_reported_when_dirty() {
    for (dirty, scope) in [
        (false, "absolute"),
        (true, "absolute"),
        (false, "relative"),
        (false, "inherited"),
    ] {
        let tmp = fixture();
        let hooks = tmp.path().join("checkout");
        fs::create_dir_all(hooks.join("tools/hooks")).unwrap();
        let caller = tmp.path().join("caller");
        git(tmp.path(), &["clone", "-q", "source", "caller"]);
        if scope == "inherited" {
            fs::write(
                tmp.path().join(".gitconfig"),
                format!(
                    "[core]\n hooksPath = {}\n",
                    hooks.join("tools/hooks").display()
                ),
            )
            .unwrap();
        } else {
            let target = if scope == "relative" {
                "../checkout/tools/hooks".into()
            } else {
                hooks.join("tools/hooks").to_str().unwrap().to_owned()
            };
            git(&caller, &["config", "core.hooksPath", &target]);
        }
        let before = git(&hooks, &["rev-parse", "HEAD"]);
        if dirty {
            fs::write(hooks.join("file"), "dirty").unwrap();
        }
        let out = Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"))
            .current_dir(tmp.path())
            .env("HOME", tmp.path())
            .env(
                "PATH",
                format!(
                    "{}:{}",
                    tmp.path().join("bin").display(),
                    std::env::var("PATH").unwrap()
                ),
            )
            .args(["--repo", "caller"])
            .output()
            .unwrap();
        let report = String::from_utf8(out.stdout).unwrap();
        assert!(report.contains("HOOK"), "{report}");
        if dirty {
            assert_eq!(out.status.code(), Some(1), "{report}");
            assert!(report.contains("reason=tracked-changes"), "{report}");
            assert_eq!(git(&hooks, &["rev-parse", "HEAD"]), before);
        } else {
            assert!(out.status.success(), "{report}");
            assert_eq!(
                git(&hooks, &["rev-parse", "HEAD"]),
                git(&tmp.path().join("source"), &["rev-parse", "HEAD"])
            );
        }
    }
}

#[cfg(unix)]
#[test]
fn symlinked_hook_checkout_is_discovered() {
    let tmp = fixture();
    let hooks = tmp.path().join("checkout");
    fs::write(hooks.join("pre-push"), "fixture hook").unwrap();
    git(tmp.path(), &["clone", "-q", "source", "caller"]);
    let caller = tmp.path().join("caller");
    let git_hooks = caller.join(".git/hooks");
    std::os::unix::fs::symlink(hooks.join("pre-push"), git_hooks.join("pre-push")).unwrap();
    let out = run(tmp.path(), &["--repo", "caller", "--repo", "checkout"]);
    assert!(out.status.success(), "{:?}", out);
    assert!(
        String::from_utf8_lossy(&out.stdout).contains("HOOK"),
        "{:?}",
        out
    );
    assert_eq!(
        git(&hooks, &["rev-parse", "HEAD"]),
        git(&tmp.path().join("source"), &["rev-parse", "HEAD"])
    );
}

#[test]
fn pre_push_prints_hook_head_and_refuses_day_old_missing_commits() {
    let tmp = fixture();
    let hooks = tmp.path().join("checkout");
    // Cached origin/main is stale too: the hook must refresh it before judging age.
    let hook = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/hooks/pre-push");
    // Exercise the installed Python entry point using this fixture as its checkout.
    let script = "import runpy,sys; m=runpy.run_path(sys.argv[1]); m['push_main'].__globals__['TOOLS']=__import__('pathlib').Path(sys.argv[2])/'tools'; sys.exit(m['main']())";
    let invoke = || {
        test_python::command()
            .args(["-c", script])
            .arg(&hook)
            .arg(&hooks)
            .stdin(std::process::Stdio::null())
            .output()
            .unwrap()
    };
    let out = invoke();
    let report = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(out.status.code(), Some(1), "{report}");
    assert!(
        report.contains(&git(&hooks, &["rev-parse", "--short", "HEAD"])),
        "{report}"
    );
    assert!(
        report.contains("stale hook checkout") && report.contains("origin/main"),
        "{report}"
    );
    git(&hooks, &["remote", "set-url", "origin", "missing-origin"]);
    let out = invoke();
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).contains("cannot refresh hook checkout"));
    git(&hooks, &["remote", "set-url", "origin", "../source"]);
    git(&hooks, &["merge", "--ff-only", "origin/main"]);
    let out = invoke();
    assert!(out.status.success(), "{:?}", out);
    assert!(String::from_utf8(out.stdout)
        .unwrap()
        .contains("hook checkout"));
}

#[cfg(unix)]
#[test]
fn external_live_unit_checkout_is_listed_and_protected() {
    let tmp = fixture();
    git(tmp.path(), &["clone", "-q", "source", "caller"]);
    let hooks = tmp.path().join("checkout");
    fs::write(tmp.path().join("bin/systemctl"), format!("#!/bin/sh\ncase \"$*\" in *list-units*) echo 'keeper.service loaded active running Keeper';; *show*) echo '{}';; esac\n", hooks.display())).unwrap();
    let before = git(&hooks, &["rev-parse", "HEAD"]);
    let out = run(tmp.path(), &["--repo", "caller"]);
    assert!(out.status.success(), "{:?}", out);
    let report = String::from_utf8(out.stdout).unwrap();
    assert!(
        report.contains("UNIT") && report.contains("live-unit"),
        "{report}"
    );
    assert_eq!(git(&hooks, &["rev-parse", "HEAD"]), before);
}

#[cfg(unix)]
#[test]
fn external_unit_executable_checkout_is_listed_and_protected() {
    let tmp = fixture();
    git(tmp.path(), &["clone", "-q", "source", "caller"]);
    let hooks = tmp.path().join("checkout");
    fs::write(hooks.join("daemon"), "fixture executable").unwrap();
    fs::write(tmp.path().join("bin/systemctl"), format!("#!/bin/sh\ncase \"$*\" in *list-units*) echo 'keeper.service loaded active running Keeper';; *WorkingDirectory*) echo /;; *ExecStart*) echo '{{ path={}/daemon ; argv[]={}/daemon ; }}';; esac\n", hooks.display(), hooks.display())).unwrap();
    let before = git(&hooks, &["rev-parse", "HEAD"]);
    let out = run(tmp.path(), &["--repo", "caller"]);
    assert!(out.status.success(), "{:?}", out);
    let report = String::from_utf8(out.stdout).unwrap();
    assert!(
        report.contains("UNIT") && report.contains("live-unit"),
        "{report}"
    );
    assert_eq!(git(&hooks, &["rev-parse", "HEAD"]), before);
}
