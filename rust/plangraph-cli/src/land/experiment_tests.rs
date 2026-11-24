use super::experiment::*;
use super::timeline::Stage;

const DAY: i64 = 86_400;

fn row(
    subject: &str,
    head: &str,
    stage: &str,
    end: i64,
    result: &str,
    items: Option<u32>,
) -> Stage {
    Stage {
        subject: subject.into(),
        head: head.into(),
        stage: stage.into(),
        host: "h".into(),
        start: end - 60,
        end,
        result: result.into(),
        items,
        run: None,
        lineage: vec![],
    }
}

/// A train with `runs` heads in CI (all but the last red), merged at `end`.
fn landed(name: &str, runs: u32, end: i64, items: Option<u32>) -> Vec<Stage> {
    let mut rows = Vec::new();
    for run in 0..runs {
        let result = if run + 1 == runs {
            "success"
        } else {
            "failure"
        };
        rows.push(row(
            name,
            &format!("{name}-{run}"),
            "ci:rust",
            end - 600 + i64::from(run),
            result,
            None,
        ));
    }
    rows.push(row(name, "tip", "merge", end, "success", items));
    rows
}

#[test]
fn the_three_numbers_are_measured_per_window_and_compared_with_the_window_before() {
    let now = 10 * DAY;
    let mut ledger = Vec::new();
    // Previous 3 days: one batch with two runs, one red run.
    ledger.extend(landed("old", 2, now - 5 * DAY, Some(3)));
    // Recent 3 days: three batches in parallel; one needed three runs, one item count is missing.
    ledger.extend(landed("a", 1, now - 2 * DAY, Some(4)));
    ledger.extend(landed("b", 3, now - DAY, Some(2)));
    ledger.extend(landed("c", 1, now - 3600, None));
    // A red train that never landed counts as red but not as a landed batch.
    ledger.push(row("lost", "x", "ci:rust", now - 100, "failure", None));
    let c = compare(&ledger, now, 3);
    assert_eq!(c.recent.landed, 3);
    assert_eq!(c.recent.items, 6);
    assert_eq!(c.recent.items_unknown, 1);
    assert!((c.recent.batches_per_hour - 3.0 / 72.0).abs() < 1e-9);
    assert!((c.recent.test_runs_per_batch - 5.0 / 3.0).abs() < 1e-9);
    assert_eq!(
        c.recent.red_trains, 3,
        "two failed heads of b and the lost one"
    );
    assert!((c.recent.red_per_day - 1.0).abs() < 1e-9);
    assert_eq!(c.previous.landed, 1);
    assert!((c.previous.test_runs_per_batch - 2.0).abs() < 1e-9);
    assert!(c.keep(), "three batches against one: batches per hour rose");
    let text = render(&c).join("\n");
    assert!(
        text.contains("recent (3d): 3 batches") && text.ends_with("keep"),
        "{text}"
    );
}

#[test]
fn an_empty_ledger_reports_zero_and_does_not_keep() {
    let c = compare(&[], 10 * DAY, 3);
    assert_eq!(c.recent.landed, 0);
    assert_eq!(c.recent.test_runs_per_batch, 0.0);
    assert!(!c.keep());
}

#[test]
fn the_experiment_command_reads_the_ledger_and_the_items_flag_is_recorded() {
    let home = tempfile::tempdir().unwrap();
    let ledger = home
        .path()
        .join(".local/state/plangraph/land-timeline.jsonl");
    let now = 10 * DAY;
    super::timeline::append(
        &ledger,
        &row("a", "h1", "merge", now - 100, "success", Some(2)),
    )
    .unwrap();
    let rows = super::timeline::load(&ledger).unwrap();
    assert_eq!(rows[0].items, Some(2));
    let args: Vec<String> = ["experiment", "--now", &now.to_string()]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
    assert_eq!(super::timeline::run(home.path(), &args), 0);
    let record: Vec<String> = [
        "record",
        "--subject",
        "b",
        "--head",
        "h",
        "--stage",
        "merge",
        "--host",
        "w",
        "--start",
        "1",
        "--end",
        "2",
        "--result",
        "success",
        "--items",
        "5",
    ]
    .iter()
    .map(|s| (*s).to_owned())
    .collect();
    assert_eq!(super::timeline::run(home.path(), &record), 0);
    assert_eq!(super::timeline::load(&ledger).unwrap()[1].items, Some(5));
}

fn ci(subject: &str, head: &str, run: &str, end: i64) -> Stage {
    Stage {
        run: Some(run.into()),
        ..row(subject, head, "ci:rust", end, "success", None)
    }
}

#[test]
fn a_retry_of_the_same_head_and_the_runs_of_combined_predecessors_are_test_runs() {
    let now = 10 * DAY;
    let mut ledger = vec![
        // The first run of the head was red and rerun: two runs, one head.
        Stage {
            result: "failure".into(),
            ..ci("big", "h1", "100/1", now - 900)
        },
        ci("big", "h1", "100/2", now - 800),
        row("big", "tip", "merge", now - 100, "success", Some(3)),
    ];
    // Two trains that were combined into `big`; each ran its own CI, one needed a rerun too.
    ledger.extend([
        ci("t1", "a", "1/1", now - 5000),
        ci("t2", "b", "2/1", now - 4000),
        ci("t2", "b", "2/2", now - 3900),
    ]);
    ledger[2].lineage = vec!["t1".into(), "t2".into()];
    let n = measure(&ledger, now - 3 * DAY, now);
    assert_eq!(n.landed, 1);
    assert_eq!(
        n.test_runs_per_batch, 5.0,
        "two of big, one of t1, two of t2"
    );
    // A predecessor of a predecessor counts too, and a train outside the lineage does not.
    ledger.push(ci("t0", "z", "0/1", now - 6000));
    ledger.push(Stage {
        lineage: vec!["t0".into()],
        ..row("t1", "a", "merge-less", now - 4900, "success", None)
    });
    ledger.push(ci("other", "o", "9/1", now - 3000));
    assert_eq!(
        measure(&ledger, now - 3 * DAY, now).test_runs_per_batch,
        6.0
    );
}
