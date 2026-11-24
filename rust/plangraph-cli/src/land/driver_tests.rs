//! The driver on real trains: landing is left to the GO gate, a remerge and a combine and a drop run.
use super::build::verify_manifest_with;
use super::build_tests::{adopted, conflicting, g, lines, part, resolve, Fx};
use super::chain::check_push;
use super::cli::run_inner;
use super::driver::{drive, Ctx, DriveTrain, Outcome};
use super::go_verify::*;
use super::main_state::State;
use super::parallel::{Ci, TrainState};
use super::parallel_tests::{land, state, train, Built};
use super::remerge::{publish_swap, remerge_verified};
use super::train::Manifest;
use std::path::Path;

fn drive_train(name: &str, main_in: &str, ci: Ci, built: &Built) -> DriveTrain {
    DriveTrain {
        state: state(name, main_in, ci),
        manifest: built.manifest.clone(),
        parts: built.parts.clone(),
        adopted: vec![],
    }
}

fn ctx<'a>(fx: &'a Fx, tip: &'a str, out: &'a Path, main: &'a State) -> Ctx<'a> {
    Ctx {
        repo: fx.path(),
        main_tip: tip,
        main,
        grant_digest: "grant",
        snapshot_position: 1,
        out,
        protected: &[],
    }
}

fn set_main(fx: &Fx, sha: &str) {
    g(fx.path(), &["update-ref", "refs/remotes/origin/main", sha]);
}

#[test]
fn landing_remerging_and_the_fresh_ci_requirement_run_through_the_driver() {
    let fx = Fx::new();
    let m0 = fx.base();
    set_main(&fx, &m0);
    let out = tempfile::tempdir().unwrap();
    let green = State::Green;
    let one = train(&fx, "t1", &m0, 1, &["a.txt"]);
    let two = train(&fx, "t2", &m0, 5, &["b.txt"]);
    let both = [
        drive_train("t1", &m0, Ci::Green, &one),
        drive_train("t2", &m0, Ci::Green, &two),
    ];
    // The first green train is handed to the GO gate alone; nothing else moves.
    let first = drive(&ctx(&fx, &m0, out.path(), &green), &both).unwrap();
    assert_eq!(
        first,
        vec![Outcome::Land {
            train: "t1".into(),
            head: one.manifest.train_sha.clone()
        }]
    );
    let m1 = land(&fx, &m0, &one.manifest.train_sha);
    set_main(&fx, &m1);
    // Main moved: the second train meets it, its branch and manifest move together.
    let second = drive(
        &ctx(&fx, &m1, out.path(), &green),
        &[drive_train("t2", &m0, Ci::Green, &two)],
    )
    .unwrap();
    let [Outcome::Remerged { head, .. }] = second.as_slice() else {
        panic!("{second:?}")
    };
    assert_eq!(g(fx.path(), &["rev-parse", "land/train-t2"]), *head);
    let written: Manifest =
        super::cli::read(&out.path().join("t2.manifest.json").to_string_lossy()).unwrap();
    assert_eq!(written.train_sha, *head);
    assert_eq!(written.parts, two.manifest.parts, "no part review again");
    let heads = fx
        .path()
        .join(".git/plangraph/train-manifests/land/train-t2.heads");
    check_push(fx.path(), head, &heads).unwrap();

    // The GO gate gets the replay's own output for the new head: with the CI run and the delta review of
    // that head it has no refusal for the current main, and `stale_base` once main moves again.
    let delta = verify_manifest_with(fx.path(), &written, &two.parts, &[], &[]).unwrap();
    let p = Pins::protected().unwrap();
    let evidence = |ci_head: &str, delta_head: &str, main: &str| {
        let jobs = p.matrix.tiers["self"]
            .keys()
            .map(|n| GoJob {
                name: n.clone(),
                conclusion: Some("success".into()),
                attempt: 1,
            })
            .collect();
        Evidence {
            repo: "o/n".into(),
            pr: 2,
            base: delta.base.clone(),
            head: head.clone(),
            tree: "t".repeat(40),
            manifest: "m".into(),
            observed: Observed {
                repo: "o/n".into(),
                pr: 2,
                pr_head: head.clone(),
                pr_base: main.into(),
                main_tip: main.into(),
            },
            replay: delta.replay("m"),
            ci: Ci2 {
                head: ci_head,
                jobs,
                p: &p,
            }
            .build(),
            approvals: vec![Approval {
                d_id: "D-100".into(),
                repo: "o/n".into(),
                pr: 2,
                head: head.clone(),
            }],
            delta: Some(DeltaAcceptance {
                run_id: format!("run-{}", "ab".repeat(16)),
                head: delta_head.into(),
                manifest: "m".into(),
                accepted: true,
            }),
            changed_paths: vec![],
            grants: vec![],
            main: State::Green,
        }
    };
    let codes = |e: &Evidence| verify(e, &p).iter().map(|r| r.code).collect::<Vec<_>>();
    assert_eq!(delta.main_in, m1);
    assert_eq!(codes(&evidence(head, head, &m1)), Vec::<&str>::new());
    assert_eq!(
        codes(&evidence(head, head, &"9".repeat(40))),
        ["stale_base"]
    );
    let old = &two.manifest.train_sha;
    let refused = codes(&evidence(old, old, &m1));
    assert!(
        refused.contains(&"ci_unbound") && refused.contains(&"delta_unbound"),
        "{refused:?}"
    );
}

struct Ci2<'a> {
    head: &'a str,
    jobs: Vec<GoJob>,
    p: &'a Pins,
}

impl Ci2<'_> {
    fn build(self) -> super::go_verify::Ci {
        super::go_verify::Ci {
            repo: "o/n".into(),
            workflow: WORKFLOW_PATH.into(),
            workflow_sha256: self.p.workflow_sha256.clone(),
            event: EVENT.into(),
            run_id: 5,
            attempt: 1,
            latest_attempt: 1,
            head: self.head.into(),
            tree: "t".repeat(40),
            tier: "self".into(),
            jobs: self.jobs,
        }
    }
}

#[test]
fn waiting_trains_are_combined_and_a_red_train_is_rebuilt_without_its_part_by_the_driver() {
    let fx = Fx::new();
    let m0 = fx.base();
    let landed = train(&fx, "t0", &m0, 1, &["a.txt"]);
    let t1 = train(&fx, "t1", &m0, 10, &["b.txt", "c.txt"]);
    let t2 = train(&fx, "t2", &m0, 20, &["d.txt"]);
    let m1 = land(&fx, &m0, &landed.manifest.train_sha);
    set_main(&fx, &m1);
    let out = tempfile::tempdir().unwrap();
    let green = State::Green;
    let done = drive(
        &ctx(&fx, &m1, out.path(), &green),
        &[
            drive_train("t1", &m0, Ci::Green, &t1),
            drive_train("t2", &m0, Ci::Running, &t2),
        ],
    )
    .unwrap();
    let [Outcome::Combined {
        build,
        head,
        trains,
    }] = done.as_slice()
    else {
        panic!("{done:?}")
    };
    assert_eq!(trains, &["t1", "t2"]);
    assert_eq!(
        g(fx.path(), &["rev-parse", &format!("land/train-{build}")]),
        *head
    );
    let big: Manifest = super::cli::read(
        &out.path()
            .join(format!("{build}.manifest.json"))
            .to_string_lossy(),
    )
    .unwrap();
    assert_eq!((big.parts.len(), big.base.as_str()), (3, m1.as_str()));

    // A red train: the hinted part is evicted, the rest is rebuilt on live main.
    let out = tempfile::tempdir().unwrap();
    let dropped = drive(
        &ctx(&fx, &m1, out.path(), &green),
        &[DriveTrain {
            state: TrainState {
                name: "t1".into(),
                main_in: m1.clone(),
                ci: Ci::Red {
                    hints: vec![11],
                    log: "log".into(),
                },
            },
            ..drive_train("t1", &m1, Ci::Green, &t1)
        }],
    )
    .unwrap();
    let [Outcome::Dropped {
        evicted, rebuilt, ..
    }] = dropped.as_slice()
    else {
        panic!("{dropped:?}")
    };
    assert_eq!(evicted, &[11]);
    let [(name, _)] = rebuilt.as_slice() else {
        panic!("{rebuilt:?}")
    };
    let rebuilt: Manifest = super::cli::read(
        &out.path()
            .join(format!("{name}.manifest.json"))
            .to_string_lossy(),
    )
    .unwrap();
    assert_eq!(rebuilt.parts.iter().map(|p| p.pr).collect::<Vec<_>>(), [10]);
}

#[test]
fn a_stale_manifest_cannot_move_a_branch_another_run_advanced_and_a_failed_write_leaves_both() {
    let fx = Fx::new();
    let m0 = fx.base();
    set_main(&fx, &m0);
    let one = train(&fx, "t1", &m0, 1, &["a.txt"]);
    let two = train(&fx, "t2", &m0, 5, &["b.txt"]);
    let m1 = land(&fx, &m0, &one.manifest.train_sha);
    set_main(&fx, &m1);
    let next = remerge_verified(fx.path(), &two.manifest, &m1, &two.parts, &[], &[]).unwrap();
    let dir = tempfile::tempdir().unwrap();
    let heads = fx
        .path()
        .join(".git/plangraph/train-manifests/land/train-t2.heads");
    let before = std::fs::read(&heads).unwrap();
    // The output cannot be written: the branch and the push manifest stay as they were.
    let bad = dir.path().join("missing/dir/out.json");
    assert!(publish_swap(fx.path(), "land/train-t2", &two.manifest, &next, &bad).is_err());
    assert_eq!(
        g(fx.path(), &["rev-parse", "land/train-t2"]),
        two.manifest.train_sha
    );
    assert_eq!(std::fs::read(&heads).unwrap(), before);
    // Another run advanced the branch first: this stale manifest must not overwrite it.
    g(fx.path(), &["update-ref", "refs/heads/land/train-t2", &m1]);
    let out = dir.path().join("out.json");
    let err = publish_swap(fx.path(), "land/train-t2", &two.manifest, &next, &out).unwrap_err();
    assert!(err.contains("no longer holds"), "{err}");
    assert_eq!(g(fx.path(), &["rev-parse", "land/train-t2"]), m1);
    assert!(!out.exists(), "no output for a move that did not happen");
    assert_eq!(std::fs::read(&heads).unwrap(), before);
}

#[test]
fn a_train_with_an_adopted_resolution_is_remerged_under_its_resolutions() {
    let fx = Fx::new();
    let (x, p, m1) = conflicting(&fx);
    let r = resolve(
        &fx,
        &m1,
        &p,
        "shared.txt",
        &lines(&["line 1", "X and P", "line 3", "line 4", "line 5", "line 6"]),
    );
    let a = adopted(&m1, &p, &r);
    let parts = vec![part(1, &x, None), part(2, &p, None)];
    let mut input = fx.input(parts.clone(), "", None);
    input.resolutions = vec![a.clone()];
    let m = super::build::build(&input).unwrap().manifest.unwrap();
    set_main(&fx, &fx.base());
    let other = fx.commit(&fx.base(), "later.txt", "later\n");
    let moved = land(&fx, &fx.base(), &other);
    set_main(&fx, &moved);
    assert!(remerge_verified(fx.path(), &m, &moved, &parts, &[], &[])
        .unwrap_err()
        .starts_with("resolution"));
    let next =
        remerge_verified(fx.path(), &m, &moved, &parts, std::slice::from_ref(&a), &[]).unwrap();
    verify_manifest_with(fx.path(), &next, &parts, &[a], &[]).unwrap();
    assert_eq!(next.resolution, m.resolution);
}

#[test]
fn the_push_check_replays_a_remerge_and_refuses_one_that_dropped_a_landed_change() {
    let fx = Fx::new();
    let m0 = fx.base();
    set_main(&fx, &m0);
    let one = train(&fx, "t1", &m0, 1, &["a.txt"]);
    let two = train(&fx, "t2", &m0, 5, &["b.txt"]);
    let m1 = land(&fx, &m0, &one.manifest.train_sha);
    set_main(&fx, &m1);
    let heads = fx
        .path()
        .join(".git/plangraph/train-manifests/land/train-t2.heads");
    let args = |tip: &str| {
        [
            "check-chain",
            "--repo",
            fx.path().to_str().unwrap(),
            "--head",
            tip,
            "--heads",
            heads.to_str().unwrap(),
        ]
        .map(str::to_owned)
    };
    let python = || std::process::Command::new("false");
    // A remerge with the right parents whose tree omits everything main brought in.
    let tree = format!("{}^{{tree}}", two.manifest.train_sha);
    let hollow = g(
        fx.path(),
        &[
            "commit-tree",
            &tree,
            "-p",
            &two.manifest.train_sha,
            "-p",
            &m1,
            "-m",
            "Merge main",
        ],
    );
    let err = run_inner(fx.path(), &args(&hollow), &python).unwrap_err();
    assert!(err.contains("extra or missing edit"), "{err}");
    // The honest remerge passes through the same entry point.
    let next = remerge_verified(fx.path(), &two.manifest, &m1, &two.parts, &[], &[]).unwrap();
    run_inner(fx.path(), &args(&next.train_sha), &python).unwrap();
}

#[test]
fn an_unattributed_red_train_keeps_both_halves_as_trains_of_their_own() {
    let fx = Fx::new();
    let m0 = fx.base();
    set_main(&fx, &m0);
    let one = train(&fx, "t1", &m0, 1, &["a.txt"]);
    let two = train(&fx, "t2", &m0, 2, &["b.txt"]);
    let both = merged(&fx, &one, &two);
    let out = tempfile::tempdir().unwrap();
    let done = drive(
        &ctx(&fx, &m0, out.path(), &State::Green),
        &[DriveTrain {
            state: state(
                "big",
                &m0,
                Ci::Red {
                    hints: vec![],
                    log: "log".into(),
                },
            ),
            ..both
        }],
    )
    .unwrap();
    let [Outcome::Dropped {
        evicted, rebuilt, ..
    }] = done.as_slice()
    else {
        panic!("{done:?}")
    };
    assert!(evicted.is_empty());
    let names: Vec<&str> = rebuilt.iter().map(|(n, _)| n.as_str()).collect();
    assert_eq!(names, ["big-drop-1", "big-drop-2"]);
    for (name, head) in rebuilt {
        assert_eq!(
            g(fx.path(), &["rev-parse", &format!("land/train-{name}")]),
            *head
        );
    }
}

/// One train of the parts of two (the shape of a red combined train).
fn merged(fx: &Fx, a: &Built, b: &Built) -> DriveTrain {
    let parts: Vec<_> = a.parts.iter().chain(&b.parts).cloned().collect();
    let mut input = fx.input(parts.clone(), "", None);
    input.build = "big".into();
    let manifest = super::build::build(&input).unwrap().manifest.unwrap();
    DriveTrain {
        state: state("big", &fx.base(), Ci::Green),
        manifest,
        parts,
        adopted: vec![],
    }
}

#[test]
fn a_failing_publish_after_the_branch_moved_is_recoverable_by_retrying_the_same_remerge() {
    let fx = Fx::new();
    let m0 = fx.base();
    set_main(&fx, &m0);
    let one = train(&fx, "t1", &m0, 1, &["a.txt"]);
    let two = train(&fx, "t2", &m0, 5, &["b.txt"]);
    let m1 = land(&fx, &m0, &one.manifest.train_sha);
    set_main(&fx, &m1);
    let next = remerge_verified(fx.path(), &two.manifest, &m1, &two.parts, &[], &[]).unwrap();
    // The same remerge again is the same commit: a retry from the original manifest finds its own result.
    let again = remerge_verified(fx.path(), &two.manifest, &m1, &two.parts, &[], &[]).unwrap();
    assert_eq!(again.train_sha, next.train_sha);
    // The last step fails after the branch moved: the output path is a directory with content in it.
    let dir = tempfile::tempdir().unwrap();
    let blocked = dir.path().join("out.json");
    std::fs::create_dir_all(blocked.join("keep")).unwrap();
    assert!(publish_swap(fx.path(), "land/train-t2", &two.manifest, &next, &blocked).is_err());
    assert_eq!(
        g(fx.path(), &["rev-parse", "land/train-t2"]),
        next.train_sha
    );
    // Retrying with the original manifest completes instead of failing the compare-and-swap.
    let out = dir.path().join("retry.json");
    publish_swap(fx.path(), "land/train-t2", &two.manifest, &again, &out).unwrap();
    let written: Manifest = super::cli::read(&out.to_string_lossy()).unwrap();
    assert_eq!(written.train_sha, next.train_sha);
    let heads = fx
        .path()
        .join(".git/plangraph/train-manifests/land/train-t2.heads");
    check_push(fx.path(), &next.train_sha, &heads).unwrap();
}
