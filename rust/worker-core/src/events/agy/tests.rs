use super::*;
use crate::activity::{Limits, Mode, Modes, Monitor};
use crate::hosts::Role;
use serde_json::json;

pub(super) const FIXTURE: &str = include_str!("../../../tests/fixtures/agy/agy-text-run.jsonl");
pub(super) fn parser() -> AgyParser {
    AgyParser::new(0, Some(180), "1.4.12")
}
pub(super) fn kinds(signals: &[Signal]) -> Vec<(Kind, Option<(ToolClass, Phase)>)> {
    signals.iter().map(|s| (s.kind, s.tool)).collect()
}
pub(super) fn send(p: &mut AgyParser, v: Value, t: u64) -> Vec<Signal> {
    p.feed(format!("{v}\n").as_bytes(), t)
}
pub(super) fn step(index: u64, name: &str) -> Value {
    json!({"event":"step_update","step_update":{"step_index":index,"step_type":name,"state":"ACTIVE"}})
}
pub(super) fn monitor(role: Role, limits: Limits) -> Monitor {
    Monitor::new(
        role,
        &limits,
        Modes {
            first: Mode::On,
            silent: Mode::On,
            milestone: Mode::On,
        },
        0,
    )
}
#[test]
fn agy_init_is_request() {
    assert_eq!(
        kinds(&send(&mut parser(), json!({"event":"init"}), 1)),
        vec![(Kind::Request, None)]
    );
}
#[test]
fn agy_user_input_is_request_and_agent_response_is_content() {
    let mut p = parser();
    assert_eq!(
        kinds(&send(&mut p, step(0, "user_input"), 1)),
        vec![(Kind::Request, None)]
    );
    assert_eq!(
        kinds(&send(&mut p, step(1, "agent_response"), 2)),
        vec![(Kind::Content, None)]
    );
}
#[test]
fn agy_fixture_run_signals() {
    let mut p = parser();
    let got: Vec<_> = FIXTURE
        .lines()
        .enumerate()
        .flat_map(|(i, l)| p.feed(format!("{l}\n").as_bytes(), 1000 * (i as u64 + 1)))
        .collect();
    assert_eq!(
        kinds(&got),
        [
            Kind::Request,
            Kind::Request,
            Kind::Content,
            Kind::Content,
            Kind::Content,
            Kind::Content,
            Kind::Request
        ]
        .map(|k| (k, None))
    );
    assert_eq!(
        (p.health().lines(), p.health().bad(), p.health().unknown()),
        (8, 0, 0)
    );
}
#[test]
fn identical_repeat_without_delta_is_not_progress() {
    let mut p = parser();
    assert_eq!(send(&mut p, step(1, "agent_response"), 1).len(), 1);
    assert!(send(&mut p, step(1, "agent_response"), 2).is_empty());
    assert_eq!(p.health().parsed(), 2);
}
#[test]
fn repeat_with_text_delta_is_content() {
    let mut p = parser();
    send(&mut p, step(1, "agent_response"), 1);
    let mut v = step(1, "agent_response");
    v["step_update"]["text_delta"] = json!("next");
    assert_eq!(kinds(&send(&mut p, v, 2)), vec![(Kind::Content, None)]);
}
#[test]
fn new_step_index_with_same_type_and_state_is_progress() {
    let mut p = parser();
    for i in [1, 2] {
        assert_eq!(
            kinds(&send(&mut p, step(i, "agent_response"), i)),
            vec![(Kind::Content, None)]
        );
    }
}
#[test]
fn unknown_step_type_is_tool_other_done() {
    let got = send(&mut parser(), step(1, "run_command"), 1);
    assert_eq!(
        kinds(&got),
        vec![(Kind::Tool, Some((ToolClass::Other, Phase::Done)))]
    );
    let mut m = monitor(Role::Implement, Limits::default());
    for s in got {
        m.feed(s);
    }
    assert_eq!(m.open_tools(), 0);
}
#[test]
fn result_success_is_request() {
    assert_eq!(
        kinds(&send(
            &mut parser(),
            json!({"event":"result","result":{"status":"SUCCESS"}}),
            1
        )),
        vec![(Kind::Request, None)]
    );
}
#[test]
fn result_error_status_is_error() {
    assert_eq!(
        kinds(&send(
            &mut parser(),
            json!({"event":"result","result":{"status":"ERROR"}}),
            1
        )),
        vec![(Kind::Error, None)]
    );
}
#[test]
fn step_update_without_step_type_is_unknown_request() {
    let mut p = parser();
    assert_eq!(
        kinds(&send(&mut p, json!({"event":"step_update"}), 1)),
        vec![(Kind::Request, None)]
    );
    assert_eq!(p.health().unknown(), 1);
}
#[test]
fn agy_recorded_quota_run_is_error() {
    let mut p = parser();
    let got: Vec<_> = include_str!("../../../tests/fixtures/agy/agy-quota-run.jsonl")
        .lines()
        .enumerate()
        .flat_map(|(i, l)| p.feed(format!("{l}\n").as_bytes(), 1000 * (i as u64 + 1)))
        .collect();
    assert_eq!(
        kinds(&got),
        vec![
            (Kind::Request, None),
            (Kind::Request, None),
            (Kind::Tool, Some((ToolClass::Other, Phase::Done))),
            (Kind::Error, None)
        ]
    );
    assert_eq!((p.health().bad(), p.health().unknown()), (0, 0));
}
