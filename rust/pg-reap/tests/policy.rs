use pg_reap::{classify, parse_porcelain, Classification::*, Facts};

fn facts() -> Facts {
    serde_json::from_str(r#"{"idle_hours":25,"clean":true,"merged_into_main":true,"has_claim":false,"live_run":false,"open_pr":false,"in_use_by_process":false,"in_use_by_unit":false,"is_clone_path":false}"#).unwrap()
}

#[test]
fn every_idle_boundary() {
    let trees =
        parse_porcelain("worktree /wt\nHEAD abc\nbranch refs/heads/attempt/card/1\n").unwrap();
    for (clean, merged, hours, eligible, reason) in [
        (true, true, 23, false, "idle-below-24h"),
        (true, true, 24, true, "merged-clean-24h"),
        (true, true, 25, true, "merged-clean-24h"),
        (true, false, 71, false, "idle-below-72h"),
        (true, false, 72, true, "unmerged-clean-72h"),
        (true, false, 73, true, "unmerged-clean-72h"),
        (false, true, 144, false, "idle-below-7d"),
        (false, false, 167, false, "idle-below-7d"),
        (false, true, 168, true, "dirty-7d-needs-backup"),
        (false, false, 192, true, "dirty-7d-needs-backup"),
    ] {
        let mut f = facts();
        (f.clean, f.merged_into_main, f.idle_hours) = (clean, merged, hours);
        let expected = if eligible {
            Eligible { rule: reason }
        } else {
            Protected { reason }
        };
        assert_eq!(classify(&trees[0], &f, &trees), expected, "{f:?}");
    }
}

#[test]
fn each_protection_beats_every_eligible_rule() {
    let trees = parse_porcelain("worktree /a\nHEAD abc\nbranch refs/heads/lane/a\n\nworktree /b\nHEAD def\nbranch refs/heads/lane/a\n").unwrap();
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
            let count = 1 + usize::from(reason == "same-branch-worktree");
            let inventory = &trees[..count];
            for w in inventory {
                assert_eq!(classify(w, &f, inventory), Protected { reason });
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
