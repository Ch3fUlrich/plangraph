//! The Claude job state file: a task count that is not a number is never read as zero.

use pg_session_resume::system::parse_job_state;

#[test]
fn a_task_count_that_is_not_a_number_is_never_read_as_zero() {
    let tasks = |text: &str| parse_job_state(text).map(|j| (j.state, j.tasks));
    let done = |flight: &str| format!(r#"{{"state":"done","inFlight":{flight}}}"#);
    assert_eq!(
        tasks(&done(
            r#"{"tasks":0,"queued":0,"kinds":[],"drainableMonitors":0}"#
        )),
        Some(("done".into(), Some(0)))
    );
    assert_eq!(
        tasks(&done(r#"{"tasks":2}"#)),
        Some(("done".into(), Some(2)))
    );
    assert_eq!(
        tasks(&done(r#"{"tasks":0,"queued":1}"#)),
        Some(("done".into(), Some(1)))
    );
    // counts whose sum overflows saturate: never a panic, never a wrap to zero
    let huge = u64::MAX;
    for flight in [
        format!(r#"{{"tasks":{huge},"queued":1}}"#),
        format!(r#"{{"tasks":1,"queued":{huge}}}"#),
        format!(r#"{{"tasks":0,"queued":{huge},"drainableMonitors":1}}"#),
    ] {
        assert_eq!(
            tasks(&done(&flight)),
            Some(("done".into(), Some(huge))),
            "{flight}"
        );
    }
    for bad in [
        r#"{"tasks":"2"}"#,
        r#"{"tasks":-1}"#,
        r#"{"tasks":null}"#,
        r#"{"queued":0}"#,
        r#"{"tasks":0,"queued":"1"}"#,
        r#"{"tasks":0,"drainableMonitors":[]}"#,
        "null",
        "7",
    ] {
        assert_eq!(tasks(&done(bad)), Some(("done".into(), None)), "{bad}");
    }
    assert_eq!(tasks(r#"{"state":"done"}"#), Some(("done".into(), None)));
    assert_eq!(tasks("{"), None);
    assert_eq!(tasks(r#"{"state":5}"#), None);
}
