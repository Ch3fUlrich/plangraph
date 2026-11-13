use super::{fs, Fixture};

#[cfg(unix)]
#[test]
fn probe_failures_after_successful_fetch_are_not_fetch_failures() {
    for (script, behind) in [
        ("if [ \"$5\" = symbolic-ref ] && [ \"$7\" = refs/remotes/origin/HEAD ]; then exit 2; fi", 1),
        ("if [ \"$5\" = symbolic-ref ] && [ \"$7\" = refs/remotes/origin/HEAD ]; then echo refs/heads/main; exit 0; fi", 1),
    ] {
        let f = Fixture::new();
        let remote_head = f.git(&f.seed, &["rev-parse", "HEAD"]);
        assert_ne!(f.git(&f.repo, &["rev-parse", "origin/main"]), remote_head);
        f.alert_command("probe-failed", behind, f.git_shim(script));
        assert_eq!(f.git(&f.repo, &["rev-parse", "origin/main"]), remote_head);
    }
}

#[cfg(unix)]
#[test]
fn rev_list_errors_after_successful_fetch_are_probe_failures() {
    for script in [
        "if [ \"$5\" = rev-list ]; then exit 128; fi",
        "if [ \"$5\" = rev-list ]; then echo invalid; exit 0; fi",
    ] {
        let f = Fixture::new();
        f.alert_command("probe-failed", 0, f.git_shim(script));
        // The zero in the alert means unavailable, not a parsed zero count.
        assert_eq!(
            f.git(&f.repo, &["rev-list", "--count", "HEAD..origin/main"]),
            b"1\n"
        );
        assert_eq!(
            f.git(&f.repo, &["rev-parse", "origin/main"]),
            f.git(&f.seed, &["rev-parse", "HEAD"])
        );
    }
}

#[test]
fn missing_default_branch_after_successful_fetch_is_probe_failure() {
    let f = Fixture::new();
    f.git(
        &f.repo,
        &[
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/missing",
        ],
    );
    f.alert("probe-failed", 0);
    assert_eq!(
        f.git(&f.repo, &["rev-parse", "origin/main"]),
        f.git(&f.seed, &["rev-parse", "HEAD"])
    );
}

#[test]
fn absent_default_ref_still_falls_back_to_main() {
    let f = Fixture::new();
    f.git(
        &f.repo,
        &["symbolic-ref", "--delete", "refs/remotes/origin/HEAD"],
    );
    f.fast_forwarded(f.run());
}

#[cfg(unix)]
#[test]
fn fetch_failure_takes_precedence_over_probe_failure() {
    let f = Fixture::new();
    f.git(&f.repo, &["fetch", "origin"]);
    for (script, behind) in [
        (
            "if [ \"$5\" = fetch ] || [ \"$5\" = rev-list ]; then exit 128; fi",
            0,
        ),
        ("if [ \"$5\" = fetch ]; then exit 128; fi", 1),
    ] {
        let before = f.snapshot();
        let out = f.git_shim(script).output().unwrap();
        assert_eq!(out.status.code(), Some(1), "{out:?}");
        assert_eq!(
            out.stdout,
            format!("ALERT repo behind={behind} reason=fetch-failed\n").as_bytes()
        );
        assert!(out.stderr.is_empty(), "{out:?}");
        assert_eq!(f.snapshot(), before);
    }
}

#[cfg(unix)]
#[test]
fn parsed_zero_behind_is_silent_despite_probe_errors() {
    let f = Fixture::new();
    f.fast_forwarded(f.run());
    for script in [
        "if [ \"$5\" = symbolic-ref ] && [ \"$7\" = refs/remotes/origin/HEAD ]; then exit 128; fi",
        "if [ \"$5\" = symbolic-ref ] && [ \"$7\" = refs/remotes/origin/HEAD ]; then echo refs/heads/main; exit 0; fi",
        "if [ \"$5\" = symbolic-ref ] || [ \"$5\" = ls-files ] || [ \"$5\" = status ]; then exit 128; fi",
    ] {
        let before = f.snapshot();
        f.fast_forwarded(f.git_shim(script).output().unwrap());
        assert_eq!(f.snapshot(), before);
    }
}

fn flagged_index_alerts_without_changes(flags: &[&str], tag: u8) {
    for edited in [true, false] {
        let f = Fixture::new();
        for flag in flags {
            f.git(&f.repo, &["update-index", flag, "tracked"]);
        }
        if edited {
            fs::write(f.repo.join("tracked"), "local edits").unwrap();
        }
        assert_eq!(f.git(&f.repo, &["ls-files", "-v", "-z"])[0], tag);
        assert!(f
            .git(
                &f.repo,
                &["status", "--porcelain=v1", "--untracked-files=no"]
            )
            .is_empty());
        f.alert("tracked-changes", 1);
    }
}

#[test]
fn skip_worktree_entries_alert_without_changes() {
    flagged_index_alerts_without_changes(&["--skip-worktree"], b'S');
}

#[test]
fn assume_unchanged_entries_alert_without_changes() {
    flagged_index_alerts_without_changes(&["--assume-unchanged"], b'h');
}

#[test]
fn lowercase_skip_worktree_entries_alert_without_changes() {
    flagged_index_alerts_without_changes(&["--skip-worktree", "--assume-unchanged"], b's');
}

#[cfg(unix)]
#[test]
fn index_flag_probe_errors_refuse_fast_forward() {
    let f = Fixture::new();
    f.alert_command(
        "probe-failed",
        1,
        f.git_shim("if [ \"$5\" = ls-files ]; then exit 128; fi"),
    );
}

#[cfg(unix)]
#[test]
fn status_probe_errors_refuse_fast_forward() {
    let f = Fixture::new();
    f.alert_command(
        "probe-failed",
        1,
        f.git_shim("if [ \"$5\" = status ]; then exit 128; fi"),
    );
}

#[test]
fn sparse_checkout_exclusions_are_conservatively_refused() {
    let f = Fixture::new();
    f.git(
        &f.repo,
        &["sparse-checkout", "set", "--no-cone", "/included/"],
    );
    assert!(!f.repo.join("tracked").exists());
    assert_eq!(f.git(&f.repo, &["ls-files", "-v", "-z"])[0], b'S');
    f.alert("tracked-changes", 1);
    assert!(!f.repo.join("tracked").exists());
}

#[cfg(unix)]
#[test]
fn newline_in_tracked_filename_does_not_fake_an_index_flag() {
    let f = Fixture::new();
    f.commit(&f.seed, "line\nS fake flag", "local contents");
    f.git(&f.seed, &["push", "origin", "main"]);
    f.fast_forwarded(f.run());
    f.commit(&f.seed, "incoming", "remote contents");
    f.git(&f.seed, &["push", "origin", "main"]);
    f.fast_forwarded(f.run());
    assert_eq!(
        fs::read_to_string(f.repo.join("line\nS fake flag")).unwrap(),
        "local contents"
    );
}
