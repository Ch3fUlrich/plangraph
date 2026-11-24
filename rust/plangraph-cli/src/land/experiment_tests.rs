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
