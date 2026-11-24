//! Parallel landing trains: the plan, the remerge, the combine and the red-train drop.
use super::build::{build, publish, verify_manifest_with, verify_result};
use super::build_tests::{g, part, Fx};
use super::chain::check_push;
use super::combine::combine_parts;
use super::parallel::{plan, Ci, Step, TrainState};
use super::remerge::remerge;
use super::split::{Action, Session};
use super::train::{Manifest, PartIn};

fn state(name: &str, main_in: &str, ci: Ci) -> TrainState {
    TrainState {
        name: name.into(),
        main_in: main_in.into(),
        ci,
    }
}

fn red(hints: &[u64]) -> Ci {
    Ci::Red {
        hints: hints.to_vec(),
        log: "log".into(),
    }
}

struct Built {
    manifest: Manifest,
    parts: Vec<PartIn>,
}

/// A train of one file-disjoint part per file, built on `base`.
fn train(fx: &Fx, name: &str, base: &str, first_pr: u64, files: &[&str]) -> Built {
    let parts: Vec<PartIn> = files
        .iter()
        .zip(first_pr..)
        .map(|(file, pr)| part(pr, &fx.commit(base, file, &format!("{file}\n")), None))
        .collect();
    let mut input = fx.input(parts.clone(), "", None);
    input.build = name.into();
    input.base = base.into();
    let manifest = build(&input).unwrap().manifest.unwrap();
    publish(fx.path(), &format!("land/build/{name}"), &manifest).unwrap();
    Built { manifest, parts }
}

/// Lands a train on main as a merge commit and points origin/main at it.
fn land(fx: &Fx, tip: &str, sha: &str) -> String {
    g(fx.path(), &["checkout", "-q", "--detach", tip]);
    g(
        fx.path(),
        &["merge", "--no-ff", "-q", "-m", "Landing train", sha],
    );
    let landed = g(fx.path(), &["rev-parse", "HEAD"]);
    g(fx.path(), &["checkout", "-q", "main"]);
    g(
        fx.path(),
        &["update-ref", "refs/remotes/origin/main", &landed],
    );
    landed
}

#[test]
fn the_first_green_train_lands_and_the_other_meets_the_new_main_without_a_part_review() {
    let fx = Fx::new();
    let m0 = fx.base();
    g(fx.path(), &["update-ref", "refs/remotes/origin/main", &m0]);
    let one = train(&fx, "t1", &m0, 1, &["a.txt", "b.txt"]);
    let two = train(&fx, "t2", &m0, 3, &["c.txt", "d.txt"]);
    let trains = [state("t1", &m0, Ci::Green), state("t2", &m0, Ci::Green)];
    assert_eq!(plan(&m0, &trains), vec![Step::Land { train: "t1".into() }]);

    let m1 = land(&fx, &m0, &one.manifest.train_sha);
    let trains = [state("t2", &m0, Ci::Green)];
    assert_eq!(
        plan(&m1, &trains),
        vec![Step::Remerge { train: "t2".into() }]
    );

    let next = remerge(fx.path(), &two.manifest, &m1).unwrap();
    assert_ne!(next.train_sha, two.manifest.train_sha);
    assert_eq!(
        next.parts, two.manifest.parts,
        "no part is added or reviewed again"
    );
    assert_eq!(next.merges, two.manifest.merges);
    // Replay accepts it and hands the remerge to the diff review; the push gate takes its chain.
    let delta = verify_manifest_with(fx.path(), &next, &two.parts, &[], &[]).unwrap();
    let json = serde_json::to_value(&delta).unwrap();
    assert_eq!(json["remerges"][0]["parents"][1], m1);
    publish(fx.path(), "land/build/t2", &next).unwrap();
    let heads = fx.path().join(".git/heads.txt");
    let list: Vec<&str> = two.parts.iter().map(|p| p.head.as_str()).collect();
    std::fs::write(&heads, list.join("\n")).unwrap();
    check_push(fx.path(), &next.train_sha, &heads).unwrap();
    // After a second landing the same train meets main once more: remerges chain.
    let again = {
        let side = fx.commit(&m1, "e.txt", "e\n");
        land(&fx, &m1, &side)
    };
    let twice = remerge(fx.path(), &next, &again).unwrap();
    verify_manifest_with(fx.path(), &twice, &two.parts, &[], &[]).unwrap();
}

#[test]
fn a_remerge_refuses_main_the_train_already_has_and_a_conflict() {
    let fx = Fx::new();
    let m0 = fx.base();
    let one = train(&fx, "t1", &m0, 1, &["shared.txt"]);
    let err = remerge(fx.path(), &one.manifest, &m0).unwrap_err();
    assert!(err.contains("already contains"), "{err}");
    let other = fx.commit(&m0, "shared.txt", "other\n");
    let err = remerge(fx.path(), &one.manifest, &other).unwrap_err();
    assert!(err.contains("conflicts"), "{err}");
}

#[test]
fn three_waiting_trains_are_one_bigger_train_built_once_on_the_new_main() {
    let fx = Fx::new();
    let m0 = fx.base();
    let landed = train(&fx, "t0", &m0, 1, &["a.txt"]);
    let t1 = train(&fx, "t1", &m0, 10, &["b.txt", "c.txt"]);
    let t2 = train(&fx, "t2", &m0, 20, &["d.txt"]);
    let t3 = train(&fx, "t3", &m0, 30, &["e.txt", "f.txt"]);
    let m1 = land(&fx, &m0, &landed.manifest.train_sha);
    let trains = [
        state("t1", &m0, Ci::Green),
        state("t2", &m0, Ci::Running),
        state("t3", &m0, Ci::Green),
    ];
    assert_eq!(
        plan(&m1, &trains),
        vec![Step::Combine {
            trains: vec!["t1".into(), "t2".into(), "t3".into()]
        }]
    );
    let union = combine_parts(&[t1.manifest, t2.manifest, t3.manifest]).unwrap();
    assert_eq!(union.len(), 5);
    let mut input = fx.input(union.clone(), "", None);
    input.build = "big".into();
    input.base = m1.clone();
    let out = build(&input).unwrap();
    let big = out.manifest.unwrap();
    assert_eq!(big.parts.len(), 5);
    assert_eq!(big.base, m1);
    verify_manifest_with(fx.path(), &big, &union, &[], &[]).unwrap();
    verify_result(fx.path(), &m1, &out.result, Some(&big)).unwrap();
    // The receipts of the parts travel with them.
    assert!(big
        .parts
        .iter()
        .zip(&union)
        .all(|(p, u)| p.receipt == u.receipt));
}

#[test]
fn a_part_at_two_heads_in_two_trains_and_a_single_train_are_refused_for_combining() {
    let fx = Fx::new();
    let m0 = fx.base();
    let t1 = train(&fx, "t1", &m0, 1, &["a.txt"]);
    let mut t2 = train(&fx, "t2", &m0, 5, &["b.txt"]);
    assert!(combine_parts(std::slice::from_ref(&t1.manifest)).is_err());
    t2.manifest.parts[0].pr = 1;
    let err = combine_parts(&[t1.manifest, t2.manifest]).unwrap_err();
    assert!(err.contains("#1"), "{err}");
}

#[test]
fn a_red_train_drops_its_failing_part_and_never_blocks_the_others() {
    let fx = Fx::new();
    let m0 = fx.base();
    let bad = train(&fx, "bad", &m0, 1, &["a.txt", "b.txt"]);
    let trains = [state("bad", &m0, red(&[2])), state("ok", &m0, Ci::Green)];
    let steps = plan(&m0, &trains);
    assert_eq!(
        steps,
        vec![
            Step::DropPart {
                train: "bad".into(),
                hints: vec![2],
                log: "log".into()
            },
            Step::Land { train: "ok".into() }
        ]
    );
    // The drop is the existing red-train session: the hinted part is evicted, the rest freezes again.
    let (mut session, first) = Session::new(bad.parts.clone());
    let actions = session.red(&first.id, &[2], "log");
    assert!(actions
        .iter()
        .any(|a| matches!(a, Action::Evict { pr: 2, .. })));
    assert!(actions
        .iter()
        .any(|a| matches!(a, Action::Freeze(c) if c.parts.len() == 1)));
}

#[test]
fn running_trains_wait_and_a_stale_red_train_is_not_remerged() {
    let m0 = "0".repeat(40);
    let m1 = "1".repeat(40);
    assert_eq!(plan(&m0, &[state("t", &m0, Ci::Running)]), vec![Step::Wait]);
    let steps = plan(&m1, &[state("t", &m0, red(&[]))]);
    assert!(matches!(steps.as_slice(), [Step::DropPart { .. }]));
}

#[test]
fn the_cli_plans_combines_and_remerges_through_land_train() {
    let fx = Fx::new();
    let m0 = fx.base();
    g(fx.path(), &["update-ref", "refs/remotes/origin/main", &m0]);
    let t1 = train(&fx, "t1", &m0, 1, &["a.txt"]);
    let t2 = train(&fx, "t2", &m0, 5, &["b.txt"]);
    let dir = tempfile::tempdir().unwrap();
    let file = |name: &str, json: serde_json::Value| {
        let path = dir.path().join(name);
        std::fs::write(&path, json.to_string()).unwrap();
        path.to_string_lossy().into_owned()
    };
    let run = |args: &[&str]| {
        let args: Vec<String> = args.iter().map(|a| (*a).to_owned()).collect();
        let python = || std::process::Command::new("false");
        super::cli::run_inner(dir.path(), &args, &python)
    };
    let trains = file(
        "trains.json",
        serde_json::json!([
            {"name": "t1", "main_in": m0, "ci": "green"},
            {"name": "t2", "main_in": m0, "ci": {"red": {"hints": [5], "log": "l"}}}
        ]),
    );
    let lines = run(&["plan", "--main", &m0, "--trains", &trains]).unwrap();
    assert_eq!(lines.len(), 2);
    assert!(
        lines[0].contains("drop_part") && lines[1].contains("\"land\""),
        "{lines:?}"
    );

    let (a, b) = (
        file("a.json", serde_json::to_value(&t1.manifest).unwrap()),
        file("b.json", serde_json::to_value(&t2.manifest).unwrap()),
    );
    let out = dir.path().join("union.json").to_string_lossy().into_owned();
    run(&["combine", "--manifests", &format!("{a},{b}"), "--out", &out]).unwrap();
    let union: Vec<PartIn> = super::cli::read(&out).unwrap();
    assert_eq!(union.len(), 2);

    let m1 = land(&fx, &m0, &t1.manifest.train_sha);
    let parts = file("parts.json", serde_json::to_value(&t2.parts).unwrap());
    let next = dir.path().join("next.json").to_string_lossy().into_owned();
    let lines = run(&[
        "remerge",
        "--repo",
        &fx.path().to_string_lossy(),
        "--manifest",
        &b,
        "--main",
        &m1,
        "--parts",
        &parts,
        "--out",
        &next,
    ])
    .unwrap();
    let moved: Manifest = super::cli::read(&next).unwrap();
    assert!(lines[0].ends_with(&moved.train_sha), "{lines:?}");
    assert_eq!(
        g(fx.path(), &["rev-parse", "land/train-t2"]),
        moved.train_sha
    );
}
