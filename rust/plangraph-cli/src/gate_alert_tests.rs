use super::*;

const HEAD: &str = "0123456789abcdef0123456789abcdef01234567";

fn green(id: u64, sha: &str, updated: &str) -> Run {
    Run {
        id,
        status: "completed".into(),
        conclusion: "success".into(),
        head_sha: sha.into(),
        updated_at: updated.into(),
    }
}

fn at(text: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(text).unwrap().into()
}

const GRACE: Duration = Duration::from_secs(900);

#[test]
fn a_run_green_for_more_than_the_grace_without_a_go_request_raises_the_alert() {
    let runs = [green(7, HEAD, "2026-11-24T10:00:00Z")];
    let alert = stale_gate(&runs, HEAD, false, at("2026-11-24T10:16:00Z"), GRACE).unwrap();
    assert_eq!(
        alert,
        Alert {
            run: 7,
            head: HEAD.into(),
            green_for_secs: 960
        }
    );
}

#[test]
fn the_age_threshold_is_exclusive_and_a_go_request_suppresses_the_alert() {
    let runs = [green(7, HEAD, "2026-11-24T10:00:00Z")];
    assert_eq!(
        stale_gate(&runs, HEAD, false, at("2026-11-24T10:15:00Z"), GRACE),
        None
    );
    assert_eq!(
        stale_gate(&runs, HEAD, true, at("2026-11-24T11:00:00Z"), GRACE),
        None
    );
}

#[test]
fn a_red_pending_or_other_head_run_raises_none() {
    let late = at("2026-11-24T12:00:00Z");
    let mut red = green(7, HEAD, "2026-11-24T10:00:00Z");
    red.conclusion = "failure".into();
    assert_eq!(stale_gate(&[red], HEAD, false, late, GRACE), None);
    let mut open = green(8, HEAD, "2026-11-24T10:00:00Z");
    open.status = "in_progress".into();
    assert_eq!(stale_gate(&[open], HEAD, false, late, GRACE), None);
    let other = green(9, "ffff", "2026-11-24T10:00:00Z");
    assert_eq!(stale_gate(&[other], HEAD, false, late, GRACE), None);
    // A newer rerun that is not finished masks an older green one.
    let rerun = [
        green(7, HEAD, "2026-11-24T10:00:00Z"),
        Run {
            status: "queued".into(),
            conclusion: String::new(),
            ..green(8, HEAD, "2026-11-24T11:00:00Z")
        },
    ];
    assert_eq!(stale_gate(&rerun, HEAD, false, late, GRACE), None);
}

#[test]
fn a_go_request_must_name_the_current_head() {
    let go = vec![format!(
        "GO-REQ #5 owner/proj head {HEAD}; diff stat: 2 files"
    )];
    assert!(requests_go(&go, HEAD));
    assert!(!requests_go(&go, "ffff"));
    assert!(!requests_go(&[format!("looks good {HEAD}")], HEAD));
}
