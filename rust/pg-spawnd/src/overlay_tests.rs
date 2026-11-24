use super::*;
use pg_worker_core::reset;

const NOW: &str = "Tue, 24 Nov 2026 12:00:00 GMT";
fn now() -> i64 {
    reset::http_date(NOW).unwrap() as i64
}
fn leg(id: &str, tier: &str, family: &str) -> Value {
    json!({"id": id, "tier": tier, "harness": "h", "model": id, "family": family})
}
fn plan(legs: &[Value]) -> Value {
    json!({"legs": legs, "first_leg": legs[0], "skipped": []})
}
fn state(until: Option<i64>, observed_s: i64) -> ProviderState {
    ProviderState { until, observed_s }
}
fn ids(plan: &Value) -> Vec<&str> {
    plan["legs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| l["id"].as_str().unwrap())
        .collect()
}
fn run(plan: &mut Value, mode: Mode, states: &States) -> Option<Value> {
    apply(plan, mode, Role::Implement, &[], Some(states), now())
}

fn three() -> Value {
    plan(&[
        leg("a", "t1", "x"),
        leg("b", "t1", "y"),
        leg("c", "t2", "z"),
    ])
}

#[test]
fn off_leaves_the_plan_untouched() {
    let mut p = three();
    let before = p.clone();
    let states = States::from([("a".into(), state(Some(now() + 3600), now()))]);
    assert!(run(&mut p, Mode::Off, &states).is_none());
    assert_eq!(p, before);
}

#[test]
fn shadow_reports_and_changes_nothing_but_the_report() {
    let mut p = three();
    let states = States::from([("a".into(), state(Some(now() + 3600), now()))]);
    let report = run(&mut p, Mode::Shadow, &states).unwrap();
    assert_eq!(report["would_drop"], json!(["a"]));
    assert_eq!(ids(&p), ["a", "b", "c"]);
    assert_eq!(p["health_overlay"], report);
}

#[test]
fn on_drops_only_open_legs_and_reorders_inside_a_tier() {
    let mut p = three();
    let states = States::from([("a".into(), state(Some(now() + 3600), now()))]);
    run(&mut p, Mode::On, &states);
    assert_eq!(ids(&p), ["b", "c"]);
    assert_eq!(p["first_leg"]["id"], "b");
    assert_eq!(
        p["skipped"][0],
        json!({"leg": "a", "reason": "omniroute_open"})
    );
}

#[test]
fn a_half_open_leg_stays_and_sorts_after_a_closed_sibling() {
    let mut p = three();
    let states = States::from([("a".into(), state(Some(now() - 1), now() - 100))]);
    let report = run(&mut p, Mode::On, &states).unwrap();
    assert_eq!(ids(&p), ["b", "a", "c"]);
    assert_eq!(report["would_drop"], json!([]));
    assert_eq!(report["would_reorder"], true);
}

#[test]
fn a_no_time_state_is_open_for_24_hours_from_its_first_read() {
    let mut p = three();
    let states = States::from([("a".into(), state(None, now() - 3600))]);
    run(&mut p, Mode::On, &states);
    assert_eq!(ids(&p), ["b", "c"]);
    let mut p = three();
    let states = States::from([("a".into(), state(None, now() - 86_400))]);
    run(&mut p, Mode::On, &states);
    assert_eq!(ids(&p), ["b", "a", "c"]);
}

#[test]
fn nothing_crosses_a_tier_and_no_leg_is_added() {
    let mut p = plan(&[
        leg("a", "t1", "x"),
        leg("b", "t2", "y"),
        leg("c", "t1", "z"),
    ]);
    let states = States::from([("a".into(), state(Some(now() - 1), now() - 100))]);
    run(&mut p, Mode::On, &states);
    assert_eq!(ids(&p), ["a", "b", "c"]);
    let extra = States::from([("zzz".into(), state(Some(now() + 3600), now()))]);
    let mut p = three();
    run(&mut p, Mode::On, &extra);
    assert_eq!(ids(&p), ["a", "b", "c"]);
}

#[test]
fn a_shared_window_drops_every_member_and_separate_ones_only_their_own() {
    let open = state(Some(now() + 3600), now());
    let shared = States::from([("a".into(), open.clone()), ("b".into(), open.clone())]);
    let mut p = three();
    run(&mut p, Mode::On, &shared);
    assert_eq!(ids(&p), ["c"]);
    let separate = States::from([("a".into(), open)]);
    let mut p = three();
    run(&mut p, Mode::On, &separate);
    assert_eq!(ids(&p), ["b", "c"]);
}

#[test]
fn an_all_open_plan_is_left_as_the_adapter_made_it() {
    let open = state(Some(now() + 3600), now());
    let states = States::from([
        ("a".into(), open.clone()),
        ("b".into(), open.clone()),
        ("c".into(), open),
    ]);
    let mut p = three();
    let report = run(&mut p, Mode::On, &states).unwrap();
    assert_eq!(ids(&p), ["a", "b", "c"]);
    assert_eq!(report["all_open"], true);
}

#[test]
fn an_unreadable_provider_changes_nothing() {
    let mut p = three();
    apply(&mut p, Mode::On, Role::Implement, &[], None, now());
    assert_eq!(ids(&p), ["a", "b", "c"]);
}

#[test]
fn on_is_refused_while_route_gate_is_not_on() {
    let none = Path::new("/nonexistent/reliability.json");
    assert_eq!(mode(Some("on"), None, none, "implement"), Mode::Off);
    assert_eq!(
        mode(Some("on"), Some("shadow"), none, "implement"),
        Mode::Off
    );
    assert_eq!(mode(Some("on"), Some("on"), none, "implement"), Mode::On);
    assert_eq!(mode(Some("shadow"), None, none, "implement"), Mode::Shadow);
    assert_eq!(mode(None, None, none, "implement"), Mode::Off);
    assert_eq!(mode(Some("bogus"), None, none, "implement"), Mode::Off);
}

#[test]
fn the_switch_reads_the_config_file_per_role() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("reliability.json");
    std::fs::write(&path, r#"{"health":{"review":"shadow"},"route_gate":"on"}"#).unwrap();
    assert_eq!(mode(None, None, &path, "review"), Mode::Shadow);
    assert_eq!(mode(None, None, &path, "implement"), Mode::Off);
}
