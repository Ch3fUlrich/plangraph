//! Wait episodes: back-to-back blocking calls on one task, the time they add up to, and which
//! call may authorise a stop.

use super::round_time_tests::{explicit_wait, MIN, T0};
use serde_json::json;

#[test]
fn consecutive_blocking_calls_on_one_task_are_one_episode_named_by_its_first_call() {
    let mut t = explicit_wait("bg1", "echo started");
    t.0.pop();
    t.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.result(4_000 + 5 * MIN, "c1", None);
    t.tool(
        4_000 + 5 * MIN + 500,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let wait = t.read().open_wait(T0 + 4_000 + 8 * MIN).unwrap();
    assert_eq!(wait.call_id, "c1");
    assert_eq!(
        wait.waited_ms,
        8 * MIN - 500,
        "the time inside the chain's calls, not the gap"
    );
    // Work in between starts a new episode with its own first call and its own age.
    let mut broken = explicit_wait("bg1", "echo started");
    broken.0.pop();
    broken.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    broken.result(4_000 + 5 * MIN, "c1", None);
    broken.tool(
        4_000 + 5 * MIN + 100,
        "e1",
        "Edit",
        json!({"file_path": "a.rs"}),
    );
    broken.result(4_000 + 5 * MIN + 200, "e1", None);
    broken.tool(
        4_000 + 5 * MIN + 500,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let wait = broken.read().open_wait(T0 + 4_000 + 8 * MIN).unwrap();
    assert_eq!(wait.call_id, "c2");
    assert_eq!(wait.waited_ms, 8 * MIN - 5 * MIN - 500);
    assert!(broken
        .read()
        .episode_alive("bg1", "c2", T0 + 4_000 + 5 * MIN + 10_000));
    assert!(
        !broken
            .read()
            .episode_alive("bg1", "c2", T0 + 4_000 + 9 * MIN),
        "a long silence is no gap between calls"
    );
    assert!(!broken
        .read()
        .episode_alive("bg1", "c1", T0 + 4_000 + 5 * MIN + 10_000));
}

#[test]
fn only_time_inside_blocking_calls_counts_and_a_long_gap_starts_a_new_episode() {
    let mut t = explicit_wait("bg1", "echo started");
    t.0.pop();
    // Two five minute calls back to back, then twenty minutes of nothing, then a new call.
    t.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.result(4_000 + 5 * MIN, "c1", None);
    t.tool(
        4_000 + 5 * MIN + 2_000,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.result(4_000 + 10 * MIN, "c2", None);
    let chain = t.read().open_wait(T0 + 4_000 + 10 * MIN + 1_000);
    assert!(chain.is_none(), "no call is open");
    let at = 4_000 + 30 * MIN;
    t.tool(
        at,
        "c3",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let wait = t.read().open_wait(T0 + at + 10_000).unwrap();
    assert_eq!(
        wait.call_id, "c3",
        "a call after a long gap is a new episode"
    );
    assert_eq!(
        wait.waited_ms, 10_000,
        "the idle twenty minutes are not wait time"
    );
    // Within the gap the same chain goes on and counts only its calls.
    let mut chain = explicit_wait("bg1", "echo started");
    chain.0.pop();
    chain.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    chain.result(4_000 + 5 * MIN, "c1", None);
    chain.tool(
        4_000 + 5 * MIN + 2_000,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let wait = chain.read().open_wait(T0 + 4_000 + 7 * MIN).unwrap();
    assert_eq!(wait.call_id, "c1");
    assert_eq!(wait.waited_ms, 5 * MIN + (7 * MIN - 5 * MIN - 2_000));
}

#[test]
fn overlapping_blocking_calls_count_their_union_not_their_sum() {
    let mut t = explicit_wait("bg1", "echo started");
    t.0.pop();
    // Three calls on the task: two five-minute calls that overlap almost completely and
    // are back to back with a third that is still open. Fifteen minutes of calls, but
    // only about eight minutes of waiting.
    t.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.tool(
        6_000,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.result(4_000 + 5 * MIN, "c1", None);
    t.result(6_000 + 5 * MIN, "c2", None);
    t.tool(
        6_000 + 5 * MIN + 1_000,
        "c3",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let now = T0 + 6_000 + 5 * MIN + 1_000 + 3 * MIN;
    let wait = t.read().open_wait(now).unwrap();
    assert_eq!(wait.call_id, "c1");
    // Union: [4 s, 5 min 6 s] plus [5 min 7 s, 8 min 7 s] = 5 min 2 s + 3 min.
    assert_eq!(wait.waited_ms, (5 * MIN + 2_000) + 3 * MIN);
}

#[test]
fn a_late_result_of_an_interrupted_episode_does_not_age_the_new_one() {
    let mut t = explicit_wait("bg1", "echo started");
    t.0.pop();
    // c1 starts an episode and is still open when other work interrupts it.
    t.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.tool(5 * MIN, "e1", "Edit", json!({"file_path": "a.rs"}));
    t.result(5 * MIN + 100, "e1", None);
    // c2 starts a new episode at 5 min 10 s; then c1 returns, late, at 16 min.
    t.tool(
        5 * MIN + 10_000,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.result(16 * MIN, "c1", None);
    let now = T0 + 16 * MIN + 10_000;
    let wait = t.read().open_wait(now).unwrap();
    assert_eq!(
        wait.call_id, "c2",
        "the new episode is named by its own first call"
    );
    assert_eq!(
        wait.waited_ms,
        16 * MIN + 10_000 - (5 * MIN + 10_000),
        "only the new episode's own call counts, not the late call of the old one"
    );
}

#[test]
fn an_open_call_left_by_an_interruption_authorises_nothing_for_the_new_episode() {
    let mut t = explicit_wait("bg1", "echo started");
    t.0.pop();
    // c1 opens an episode and stays open; other work ends the episode; c2 starts another
    // and returns after 15 minutes. Nothing of the new episode is open, only c1 of the old.
    t.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.tool(5 * MIN, "e1", "Edit", json!({"file_path": "a.rs"}));
    t.result(5 * MIN + 100, "e1", None);
    t.tool(
        5 * MIN + 10_000,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.result(21 * MIN, "c2", None);
    let reader = t.read();
    assert_eq!(
        reader.open_wait(T0 + 21 * MIN + 5_000),
        None,
        "c1 belongs to an episode that is over"
    );
    // While c2 is open it is the authorising call, with its own age.
    let open = t.cut(t.0.len() - 1);
    let wait = open.read().open_wait(T0 + 21 * MIN).unwrap();
    assert_eq!(wait.call_id, "c2");
}

#[test]
fn an_interrupted_open_call_cannot_authorise_a_stop() {
    let mut t = explicit_wait("bg1", "echo started");
    t.0.pop();
    t.tool(
        4_000,
        "wait",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    assert!(t.read().open_wait(T0 + 16 * MIN).is_some());

    t.tool(16 * MIN, "edit", "Edit", json!({"file_path": "a.rs"}));
    assert_eq!(t.read().open_wait(T0 + 17 * MIN), None);
}
