//! A keeper reply that arrives after the refresh wait ended, through the real curl transport.
//! One test in this file: it changes the process PATH and environment.
#![cfg(unix)]
use plangraph_cli::feeder::Env;
use plangraph_cli::merge_close::{run_timed, ScheduleTiming};
use std::fs;
use std::time::Duration;

const SKIP: &str = "merge-close: skip, merge index refresh still running (next run retries)\n";

fn run(root: &std::path::Path, status_reply: &str) -> Result<String, String> {
    fs::write(root.join("calls"), "0\n").unwrap();
    fs::write(root.join("reply"), status_reply).unwrap();
    let mut env = Env::new();
    env.insert("PG_URL".into(), "http://127.0.0.1".into());
    env.insert(
        "PG_TOKEN_FILE".into(),
        root.join("token").display().to_string(),
    );
    let timing = ScheduleTiming::new(Duration::from_secs(1), Duration::from_secs(60));
    run_timed(&env, true, Some("demo"), false, timing).map(|out| out.text)
}

#[test]
fn a_reply_after_the_wait_keeps_its_meaning() {
    let root = tempfile::tempdir().unwrap();
    fs::write(root.path().join("token"), "fixture").unwrap();
    // Call 0 starts the refresh; call 1 is the status poll and answers after the wait ended.
    pg_worker_core::executable_test_support::write_executable(
        root.path().join("curl"),
        r#"#!/bin/sh
/bin/cat >/dev/null
read -r n < "$CALLS"
printf '%s\n' "$((n+1))" > "$CALLS"
case "$n" in
  0) printf '{"scan":"running"}\n200' ;;
  1) /bin/sleep 1.5; /bin/cat "$REPLY" ;;
  *) exit 99 ;;
esac
"#,
    );
    std::env::set_var("PATH", root.path());
    std::env::set_var("CALLS", root.path().join("calls"));
    std::env::set_var("REPLY", root.path().join("reply"));
    // A refusal that arrives late is still a refusal, not a skip.
    let error = run(root.path(), "{\"error\":\"forbidden\"}\n403").unwrap_err();
    assert_eq!(error, "keeper request refused or unreachable");
    // A status that arrives late and says ready is not inside the wait: no close starts.
    let text = run(root.path(), "{\"scan\":\"ready\"}\n200");
    assert!(text.is_ok(), "{text:?}");
    assert_eq!(
        fs::read_to_string(root.path().join("calls")).unwrap(),
        "2\n"
    );
    assert_eq!(text.unwrap(), SKIP);
}
