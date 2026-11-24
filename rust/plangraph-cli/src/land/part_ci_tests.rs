use super::build_tests::{part, Fx};
use super::part_ci::*;

fn run(pr: u64, head: &str, base: &str, conclusion: &str) -> PartCi {
    PartCi {
        pr,
        head: head.into(),
        run_id: format!("{pr}00"),
        base: base.into(),
        workflow: FULL_WORKFLOW.into(),
        conclusion: conclusion.into(),
    }
}

/// main: base -> t (the previous train); lanes branch from `t` (fresh) or from `base` (stale).
struct World {
    fx: Fx,
    old: String,
    train: String,
}

fn world() -> World {
    let fx = Fx::new();
    let old = fx.base();
    let train = fx.commit(&old, "train.txt", "t\n");
    super::build_tests::g(fx.path(), &["update-ref", "refs/heads/main", &train]);
    World { fx, old, train }
}

#[test]
fn a_part_with_a_green_full_run_at_a_base_after_the_previous_train_is_admitted() {
    let w = world();
    let head = w.fx.commit(&w.train, "a.txt", "a\n");
    let parts = vec![part(1, &head, None)];
    assert_eq!(
        refusals(
            w.fx.path(),
            &parts,
            &[run(1, &head, &w.train, "success")],
            &w.train
        )
        .unwrap(),
        Vec::<String>::new()
    );
}

#[test]
fn a_part_without_a_run_a_red_run_or_a_stale_base_is_refused() {
    let w = world();
    let fresh = w.fx.commit(&w.train, "a.txt", "a\n");
    let stale = w.fx.commit(&w.old, "b.txt", "b\n");
    let red = w.fx.commit(&w.train, "c.txt", "c\n");
    let moved = w.fx.commit(&w.train, "d.txt", "d\n");
    let parts = vec![
        part(1, &fresh, None),
        part(2, &stale, None),
        part(3, &red, None),
        part(4, &moved, None),
    ];
    let mut other_workflow = run(1, &fresh, &w.train, "success");
    other_workflow.workflow = ".github/workflows/lint.yml".into();
    let runs = [
        run(2, &stale, &w.old, "success"),
        run(3, &red, &w.train, "failure"),
        // A run of an earlier head does not cover the head that moved.
        run(4, &fresh, &w.train, "success"),
    ];
    let found = refusals(w.fx.path(), &parts, &runs, &w.train).unwrap();
    assert_eq!(found.len(), 4, "{found:#?}");
    assert!(found[0].contains("part #1") && found[0].contains("no full CI run"));
    assert!(found[1].contains("part #2") && found[1].contains("older than the previous train"));
    assert!(found[2].contains("part #3") && found[2].contains("concluded failure"));
    assert!(found[3].contains("part #4") && found[3].contains("no full CI run"));
    let wrong = refusals(w.fx.path(), &parts[..1], &[other_workflow], &w.train).unwrap();
    assert!(wrong[0].contains("is not a run of"), "{wrong:?}");
    assert!(require(w.fx.path(), &parts, &runs, &w.train).is_err());
}

#[test]
fn a_run_whose_base_is_not_under_the_part_head_is_refused() {
    let w = world();
    let head = w.fx.commit(&w.train, "a.txt", "a\n");
    let elsewhere = w.fx.commit(&w.train, "z.txt", "z\n");
    let found = refusals(
        w.fx.path(),
        &[part(1, &head, None)],
        &[run(1, &head, &elsewhere, "success")],
        &w.train,
    )
    .unwrap();
    assert!(found[0].contains("not under the part head"), "{found:?}");
}

#[test]
fn the_build_command_refuses_without_part_runs_before_it_reads_main() {
    let w = world();
    let head = w.fx.commit(&w.train, "a.txt", "a\n");
    let dir = tempfile::tempdir().unwrap();
    let parts = dir.path().join("parts.json");
    std::fs::write(
        &parts,
        serde_json::to_vec(&vec![part(1, &head, None)]).unwrap(),
    )
    .unwrap();
    let args: Vec<String> = [
        "build",
        "--repo",
        w.fx.path().to_str().unwrap(),
        "--base",
        &w.train,
        "--name",
        "t1",
        "--parts",
        parts.to_str().unwrap(),
        "--grant-digest",
        "d",
        "--snapshot-position",
        "1",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    let python = || std::process::Command::new("python3");
    let err = super::cli::run_inner(dir.path(), &args, &python).unwrap_err();
    assert!(err.contains("missing --part-ci"), "{err}");
}
