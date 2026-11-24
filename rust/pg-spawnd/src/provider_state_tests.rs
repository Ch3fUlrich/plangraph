use super::*;
use std::cell::RefCell;
use std::collections::VecDeque;

const DATE: &str = "Tue, 24 Nov 2026 12:00:00 GMT";
const PLUS_1M: &str = "Tue, 24 Nov 2026 12:01:00 GMT";

struct Fake(RefCell<VecDeque<Result<Fetched, ()>>>, RefCell<u32>);
impl Fetch for Fake {
    fn get(&self) -> Result<Fetched, ()> {
        *self.1.borrow_mut() += 1;
        self.0.borrow_mut().pop_front().unwrap_or(Err(()))
    }
}
fn ok(body: Value, date: Option<&str>) -> Result<Fetched, ()> {
    Ok(Fetched {
        body: body.to_string(),
        date: date.map(str::to_owned),
    })
}
fn reader(replies: Vec<Result<Fetched, ()>>) -> Reader<Fake> {
    Reader::new(Fake(RefCell::new(replies.into()), RefCell::new(0)))
}
fn now() -> i64 {
    reset::http_date(DATE).unwrap() as i64
}
fn window(models: &[&str], until: &str) -> Value {
    serde_json::json!({"windows":[{"models":models,"rateLimitedUntil":until}]})
}

#[test]
fn unreachable_refused_and_malformed_reads_are_ignored() {
    for reply in [
        Err(()),
        Ok(Fetched {
            body: "not json".into(),
            date: None,
        }),
        ok(serde_json::json!({"other": 1}), None),
    ] {
        assert!(reader(vec![reply]).read(now()).is_none());
    }
}

#[test]
fn a_failed_reread_serves_the_last_good_state_only_until_it_is_stale() {
    let body = window(&["m"], "2026-11-25T00:00:00Z");
    let mut r = reader(vec![ok(body, Some(DATE)), Err(()), Err(())]);
    assert!(r.read(now()).unwrap().contains_key("m"));
    assert!(r.read(now() + 60).unwrap().contains_key("m"));
    assert!(r.read(now() + 120).is_none());
}

#[test]
fn ttl_serves_a_cached_read_without_fetching() {
    let mut r = reader(vec![ok(window(&["m"], "2026-11-25T00:00:00Z"), Some(DATE))]);
    r.read(now());
    r.read(now() + 59);
    assert_eq!(*r.fetch.1.borrow(), 1);
    r.read(now() + 60);
    assert_eq!(*r.fetch.1.borrow(), 2);
}

#[test]
fn an_absolute_window_converts_via_the_response_date_and_is_never_clamped() {
    let three_days = now() + 3 * 86_400;
    let mut r = reader(vec![
        ok(window(&["m"], "2026-11-27T12:00:00Z"), Some(DATE)),
        ok(window(&["m"], "2026-11-27T12:00:00Z"), Some(PLUS_1M)),
    ]);
    let first = r.read(now()).unwrap()["m"].clone();
    assert_eq!(first.until, Some(three_days));
    let second = r.read(now() + 60).unwrap()["m"].clone();
    assert_eq!(second.until, Some(three_days));
    assert_eq!(second.observed_s, now());
}

#[test]
fn a_date_less_absolute_value_has_no_time_and_keeps_its_first_observed_s() {
    let body = window(&["m"], "2026-11-27T12:00:00Z");
    let mut r = reader(vec![ok(body.clone(), None), ok(body, None)]);
    let first = r.read(now()).unwrap()["m"].clone();
    assert_eq!((first.until, first.observed_s), (None, now()));
    let second = r.read(now() + 60).unwrap()["m"].clone();
    assert_eq!((second.until, second.observed_s), (None, now()));
}

#[test]
fn a_changed_until_takes_the_new_read_and_a_cleared_state_starts_afresh() {
    let mut r = reader(vec![
        ok(window(&["m"], "2026-11-25T00:00:00Z"), Some(DATE)),
        ok(window(&["m"], "2026-11-26T00:00:00Z"), Some(PLUS_1M)),
        ok(serde_json::json!({"windows":[]}), Some(PLUS_1M)),
        ok(window(&["m"], "2026-11-26T00:00:00Z"), Some(PLUS_1M)),
    ]);
    r.read(now());
    assert_eq!(r.read(now() + 60).unwrap()["m"].observed_s, now() + 60);
    assert!(r.read(now() + 120).unwrap().is_empty());
    assert_eq!(r.read(now() + 180).unwrap()["m"].observed_s, now() + 180);
}

#[test]
fn omniroute_change_time_is_the_observation_time() {
    let mut body = window(&["m"], "2026-11-25T00:00:00Z");
    body["windows"][0]["changedAt"] = serde_json::json!((now() - 600) * 1000);
    let state = reader(vec![ok(body, Some(DATE))]).read(now()).unwrap()["m"].clone();
    assert_eq!(state.observed_s, now() - 600);
}

#[test]
fn one_shared_window_opens_every_member_and_two_separate_ones_stay_separate() {
    let shared = reader(vec![ok(
        window(&["agy/argon", "agy/flash"], "2026-11-25T00:00:00Z"),
        Some(DATE),
    )])
    .read(now())
    .unwrap()
    .clone();
    assert_eq!(shared["agy/argon"], shared["agy/flash"]);
    let separate = serde_json::json!({"windows":[
        {"models":["agy/argon"],"rateLimitedUntil":"2026-11-25T00:00:00Z"},
        {"models":["agy/flash"]}]});
    let read = reader(vec![ok(separate, Some(DATE))])
        .read(now())
        .unwrap()
        .clone();
    assert!(read.contains_key("agy/argon") && !read.contains_key("agy/flash"));
}

#[test]
fn an_open_breaker_reads_retry_after_ms_and_a_half_open_one_is_not_a_state() {
    let body = serde_json::json!({"circuitBreakers":[
        {"name":"a","state":"OPEN","retryAfterMs":30000},
        {"name":"b","state":"HALF_OPEN","retryAfterMs":30000},
        {"name":"c","state":"OPEN"}]});
    let read = reader(vec![ok(body, Some(DATE))])
        .read(now())
        .unwrap()
        .clone();
    assert_eq!(read["a"].until, Some(now() + 30));
    assert!(!read.contains_key("b"));
    assert_eq!(read["c"].until, None);
}

#[test]
fn the_later_deadline_wins_when_a_breaker_and_a_window_name_one_model() {
    let body = serde_json::json!({
        "circuitBreakers":[{"name":"m","state":"OPEN","retryAfterMs":30000}],
        "windows":[{"models":["m"],"rateLimitedUntil":"2026-11-24T18:00:00Z"}]});
    let read = reader(vec![ok(body, Some(DATE))])
        .read(now())
        .unwrap()
        .clone();
    assert_eq!(read["m"].until, Some(now() + 6 * 3600));
}
