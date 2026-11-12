use pg_reap::{classify, parse_porcelain, state, Classification::*, Facts, State};

fn facts() -> Facts {
    serde_json::from_str(r#"{"idle_hours":25,"clean":true,"merged_into_main":true,"pushed":true,"failed":false,"has_claim":false,"live_run":false,"open_pr":false,"in_use_by_process":false,"in_use_by_unit":false,"is_clone_path":false}"#).unwrap()
}

#[test]
fn unmerged_commits_are_never_eligible() {
    let trees = parse_porcelain("worktree /main\nbare\n\nworktree /wt\nHEAD abc\ndetached\n\nworktree /branch\nHEAD def\nbranch refs/heads/lane/a\n").unwrap();
    for w in &trees[1..] {
        for clean in [false, true] {
            for idle_hours in [0, 24, 71, 72, 73, 167, 168, 192, u64::MAX] {
                let mut f = facts();
                (f.clean, f.merged_into_main, f.idle_hours) = (clean, false, idle_hours);
                for pushed in [false, true] {
                    f.pushed = pushed;
                    assert_eq!(
                        classify(w, &f, &trees),
                        Protected {
                            reason: "unmerged-commits"
                        },
                        "{f:?}"
                    );
                }
            }
        }
    }
}

#[test]
fn unpushed_commits_are_never_eligible() {
    let trees = parse_porcelain("worktree /main\nbare\n\nworktree /wt\nHEAD abc\nbranch refs/heads/lane/a\n\nworktree /detached\nHEAD def\ndetached\n").unwrap();
    let mut f = facts();
    f.pushed = false;
    for clean in [false, true] {
        for idle_hours in [0, 23, 24, 25, 167, 168, 192, u64::MAX] {
            (f.clean, f.idle_hours) = (clean, idle_hours);
            for w in &trees[1..] {
                assert_eq!(
                    classify(w, &f, &trees),
                    Protected {
                        reason: "unpushed-commits"
                    },
                    "{f:?}"
                );
            }
        }
    }
}

#[test]
fn main_worktree_is_intrinsically_protected() {
    let mut trees = parse_porcelain("worktree /z-main\nHEAD abc\nbranch refs/heads/trunk\n\nworktree /a-linked\nHEAD def\ndetached\n").unwrap();
    trees.sort_by(|a, b| a.path.cmp(&b.path));
    for clean in [false, true] {
        for merged in [false, true] {
            let mut f = facts();
            (f.clean, f.merged_into_main, f.idle_hours) = (clean, merged, u64::MAX);
            assert_eq!(
                classify(&trees[1], &f, &trees),
                Protected {
                    reason: "main-worktree"
                },
                "{f:?}"
            );
        }
    }
}

#[test]
fn empty_porcelain_fails_closed() {
    for text in ["", "\n", "\n\n", " \t\r\n\n \t\n"] {
        assert!(parse_porcelain(text).is_err(), "{text:?}");
    }
}

#[test]
fn lifecycle_labels_and_abandonment_boundary() {
    let trees =
        parse_porcelain("worktree /main\nbare\n\nworktree /wt\nHEAD abc\ndetached\n").unwrap();
    for (merged, failed, hours, expected) in [
        (true, false, 0, State::Merged),
        (true, true, 192, State::Merged),
        (false, true, 0, State::Failed),
        (false, true, 192, State::Failed),
        (false, false, 0, State::Live),
        (false, false, 71, State::Live),
        (false, false, 72, State::Abandoned),
        (false, false, 73, State::Abandoned),
    ] {
        let mut f = facts();
        (f.merged_into_main, f.failed, f.idle_hours) = (merged, failed, hours);
        assert_eq!(state(&trees[1], &f, &trees), expected, "{f:?}");
        assert_eq!(state(&trees[0], &f, &trees), State::Live);
        f.has_claim = true;
        assert_eq!(state(&trees[1], &f, &trees), State::Live);
    }
}

#[test]
fn simultaneous_protections_have_stable_precedence() {
    let trees = parse_porcelain("worktree /main\nbare\n\nworktree /a\nHEAD abc\nbranch refs/heads/lane/a\nlocked owner\nprunable stale\n\nworktree /b\nHEAD def\nbranch refs/heads/lane/a\n").unwrap();
    let mut f = facts();
    f.idle_hours = u64::MAX;
    f.merged_into_main = false;
    f.pushed = false;
    f.is_clone_path = true;
    f.in_use_by_process = true;
    f.in_use_by_unit = true;
    f.has_claim = true;
    f.live_run = true;
    f.open_pr = true;
    for reason in [
        "clone-path",
        "process-in-use",
        "unit-in-use",
        "claim",
        "live-run",
        "open-pr",
        "same-branch-worktree",
    ] {
        assert_eq!(classify(&trees[1], &f, &trees), Protected { reason });
        match reason {
            "clone-path" => f.is_clone_path = false,
            "process-in-use" => f.in_use_by_process = false,
            "unit-in-use" => f.in_use_by_unit = false,
            "claim" => f.has_claim = false,
            "live-run" => f.live_run = false,
            "open-pr" => f.open_pr = false,
            _ => (),
        }
    }
    for fields in [
        "locked owner\nprunable stale",
        "prunable stale\nlocked owner",
    ] {
        let trees = parse_porcelain(&format!(
            "worktree /main\nbare\n\nworktree /a\nHEAD abc\ndetached\n{fields}\n"
        ))
        .unwrap();
        assert_eq!(
            classify(&trees[1], &f, &trees),
            Protected { reason: "locked" }
        );
        assert_eq!(state(&trees[1], &f, &trees), State::Live);
    }
    let trees = parse_porcelain("worktree /main\nbare\nlocked owner\nprunable stale\n").unwrap();
    assert_eq!(
        classify(&trees[0], &f, &trees),
        Protected { reason: "bare" }
    );
}

#[test]
fn live_lock_beats_every_commit_and_age_combination() {
    let trees =
        parse_porcelain("worktree /main\nbare\n\nworktree /wt\nHEAD abc\ndetached\nlocked owner\n")
            .unwrap();
    for clean in [false, true] {
        for merged in [false, true] {
            for pushed in [false, true] {
                for idle_hours in [0, 24, 72, 168, u64::MAX] {
                    let mut f = facts();
                    (f.clean, f.merged_into_main, f.pushed, f.idle_hours) =
                        (clean, merged, pushed, idle_hours);
                    assert_eq!(
                        classify(&trees[1], &f, &trees),
                        Protected { reason: "locked" },
                        "{f:?}"
                    );
                    assert_eq!(state(&trees[1], &f, &trees), State::Live);
                }
            }
        }
    }
}

#[test]
fn every_idle_boundary() {
    let trees = parse_porcelain(
        "worktree /main\nbare\n\nworktree /wt\nHEAD abc\nbranch refs/heads/attempt/card/1\n",
    )
    .unwrap();
    for (clean, merged, hours, eligible, reason) in [
        (true, true, 23, false, "idle-below-24h"),
        (true, true, 24, true, "merged-clean-24h"),
        (true, true, 25, true, "merged-clean-24h"),
        (true, false, 71, false, "unmerged-commits"),
        (true, false, 72, false, "unmerged-commits"),
        (true, false, 73, false, "unmerged-commits"),
        (false, true, 144, false, "idle-below-7d"),
        (false, true, 167, false, "idle-below-7d"),
        (false, false, 167, false, "unmerged-commits"),
        (false, true, 168, true, "dirty-7d-needs-backup"),
        (false, true, 169, true, "dirty-7d-needs-backup"),
        (false, false, 192, false, "unmerged-commits"),
    ] {
        let mut f = facts();
        (f.clean, f.merged_into_main, f.idle_hours) = (clean, merged, hours);
        let expected = if eligible {
            Eligible { rule: reason }
        } else {
            Protected { reason }
        };
        assert_eq!(classify(&trees[1], &f, &trees), expected, "{f:?}");
    }
}

#[test]
fn each_protection_beats_every_eligible_rule() {
    let trees = parse_porcelain("worktree /main\nbare\n\nworktree /a\nHEAD abc\nbranch refs/heads/lane/a\n\nworktree /b\nHEAD def\nbranch refs/heads/lane/a\n").unwrap();
    for (clean, merged) in [(true, true), (true, false), (false, true), (false, false)] {
        for reason in
            "clone-path process-in-use unit-in-use claim live-run open-pr same-branch-worktree"
                .split_whitespace()
        {
            let mut f = facts();
            (f.clean, f.merged_into_main, f.idle_hours) = (clean, merged, 192);
            match reason {
                "clone-path" => f.is_clone_path = true,
                "process-in-use" => f.in_use_by_process = true,
                "unit-in-use" => f.in_use_by_unit = true,
                "claim" => f.has_claim = true,
                "live-run" => f.live_run = true,
                "open-pr" => f.open_pr = true,
                _ => (),
            }
            let count = 2 + usize::from(reason == "same-branch-worktree");
            let inventory = &trees[..count];
            for w in &inventory[1..] {
                assert_eq!(classify(w, &f, inventory), Protected { reason });
                assert_eq!(state(w, &f, inventory), State::Live);
            }
        }
    }
}

#[test]
fn porcelain_fixture_and_fail_closed_errors() {
    let fixture = "worktree /clone\nbare\n\nworktree \"/wt/sp ace\\t\\303\\251\"\nHEAD abc\nbranch refs/heads/attempt/card/1\nlocked owner\n\nworktree /detached\nHEAD def\ndetached\nprunable missing\n";
    let trees = parse_porcelain(fixture).unwrap();
    assert_eq!(trees.len(), 3);
    assert_eq!(trees[1].path, "/wt/sp ace\té");
    let branch = trees[1].branch.as_deref().unwrap();
    assert_eq!(branch, "refs/heads/attempt/card/1");
    assert_eq!(trees[1].head, "abc");
    assert_eq!(trees[2].branch, None);
    for (w, reason) in trees.iter().zip(["bare", "locked", "prunable"]) {
        assert_eq!(classify(w, &facts(), &trees), Protected { reason });
    }
    for bad in [
        "HEAD abc",
        "worktree \"bad\\q\"\nbare",
        "worktree /x\nHEAD abc",
        "worktree /x\nbare\n\nworktree /x\nbare",
    ] {
        assert!(parse_porcelain(bad).is_err(), "{bad}");
    }
}
