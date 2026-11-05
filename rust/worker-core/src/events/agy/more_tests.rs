use super::tests::{kinds, monitor, parser, send, step, FIXTURE};
use super::*;
use crate::activity::{Act, Ctx, Limits, Rule, State};
use crate::events::{BlindReason, MAX_LINE};
use crate::hosts::Role;
use serde_json::json;

#[test]
fn quota_synthetic_agy_error_result_with_resets_in_is_error() {
    let v = json!({"event":"result","result":{"status":"AGY_ERROR","error":"429 Individual quota reached. Resets in 10h40m26s."}});
    assert_eq!(kinds(&send(&mut parser(), v, 1)), vec![(Kind::Error, None)]);
}
#[test]
fn result_with_error_field_and_success_status_is_error() {
    for error in [json!("failure"), json!(false), json!({})] {
        assert_eq!(
            kinds(&send(
                &mut parser(),
                json!({"event":"result","result":{"status":"SUCCESS","error":error}}),
                1
            )),
            vec![(Kind::Error, None)]
        );
    }
    for error in [Value::Null, json!("")] {
        assert_eq!(
            kinds(&send(
                &mut parser(),
                json!({"event":"result","result":{"status":"SUCCESS","error":error}}),
                1
            )),
            vec![(Kind::Request, None)]
        );
    }
}
#[test]
fn flat_step_update_without_nested_object_uses_top_level_fields() {
    for nested in [Value::Null, json!(false), json!([])] {
        assert_eq!(
            kinds(&send(
                &mut parser(),
                json!({"event":"step_update","step_update":nested,"step_type":"agent_response","state":"ACTIVE"}),
                1
            )),
            vec![(Kind::Content, None)]
        );
    }
    assert_eq!(
        kinds(&send(
            &mut parser(),
            json!({"event":"step_update","step_type":"user_input"}),
            1
        )),
        vec![(Kind::Request, None)]
    );
    assert_eq!(
        kinds(&send(
            &mut parser(),
            json!({"event":"result","status":"ERROR"}),
            1
        )),
        vec![(Kind::Error, None)]
    );
}
#[test]
fn unknown_event_name_and_missing_event_are_unknown_request() {
    let mut p = parser();
    for v in [json!({"event":"future"}), json!({}), json!({"event":42})] {
        assert_eq!(kinds(&send(&mut p, v, 1)), vec![(Kind::Request, None)]);
    }
    assert_eq!((p.health().unknown(), p.health().parsed()), (3, 3));
}
#[test]
fn non_json_array_scalar_and_overlong_lines_are_bad_without_signals() {
    let mut p = parser();
    for line in [
        b"broken\n".as_slice(),
        b"[]\n",
        b"1\n",
        b"null\n",
        b"\xff\n",
    ] {
        assert!(p.feed(line, 1).is_empty());
    }
    assert!(p.feed(&vec![b'x'; MAX_LINE + 1], 1).is_empty());
    assert!(p.feed(b"\n", 1).is_empty());
    assert_eq!(
        (p.health().bad(), p.health().lines(), p.health().parsed()),
        (6, 6, 0)
    );
}
#[test]
fn over_half_bad_of_20_lines_goes_blind() {
    let mut p = parser();
    for _ in 0..11 {
        p.feed(b"bad\n", 1);
    }
    for _ in 0..9 {
        send(&mut p, json!({"event":"init"}), 1);
    }
    assert_eq!(p.health.blind(1), Some(BlindReason::BadRatio));
}
#[test]
fn version_drift_is_blind_and_recorded_version_is_not() {
    for version in ["1.4.12", "1.5.1", "1.4.13", ""] {
        let mut p = AgyParser::new(0, Some(180), version);
        assert_eq!(
            p.health.blind(0),
            if RECORDED.contains(&version) {
                None
            } else {
                Some(BlindReason::VersionDrift)
            }
        );
    }
}
#[test]
fn chunk_split_anywhere_equals_one_chunk() {
    let mut whole = parser();
    let expected = whole.feed(FIXTURE.as_bytes(), 1000);
    let signature = |s: &[Signal]| {
        s.iter()
            .map(|s| (s.kind, s.tool, s.ts_ms))
            .collect::<Vec<_>>()
    };
    for split in 0..=FIXTURE.len() {
        let mut p = parser();
        let mut got = p.feed(&FIXTURE.as_bytes()[..split], 1000);
        got.extend(p.feed(&FIXTURE.as_bytes()[split..], 1000));
        assert_eq!(signature(&got), signature(&expected));
        assert_eq!(
            (
                p.health().lines(),
                p.health().parsed(),
                p.health().bad(),
                p.health().unknown()
            ),
            (8, 8, 0, 0)
        );
    }
    let mut p = parser();
    let got: Vec<_> = FIXTURE.bytes().flat_map(|b| p.feed(&[b], 1000)).collect();
    assert_eq!(signature(&got), signature(&expected));
    assert_eq!(
        (
            p.health().lines(),
            p.health().parsed(),
            p.health().bad(),
            p.health().unknown()
        ),
        (8, 8, 0, 0)
    );
}
#[test]
fn finish_input_parses_unterminated_tail() {
    let mut p = parser();
    assert!(p
        .feed(json!({"event":"init"}).to_string().as_bytes(), 1)
        .is_empty());
    let got = p.finish_input(2);
    assert_eq!(kinds(&got), vec![(Kind::Request, None)]);
    assert_eq!(got[0].ts_ms, 2);
    assert!(p.finish_input(3).is_empty());
    assert_eq!(p.health().parsed(), 1);
}
#[test]
fn signal_times_are_arrival_not_event_fields() {
    let mut p = parser();
    let v = json!({"event":"init","timestamp":999999,"ts_ms":777777}).to_string();
    assert!(p.feed(v.as_bytes(), 10).is_empty());
    assert_eq!(p.feed(b"\n", 20)[0].ts_ms, 20);
    assert_eq!(send(&mut p, step(1, "agent_response"), 30)[0].ts_ms, 30);
}
#[test]
fn parser_keeps_no_payload_text() {
    let mut p = parser();
    let signals = p.feed(FIXTURE.as_bytes(), 1000);
    let debug = format!("{signals:?}");
    for line in FIXTURE.lines() {
        let v: Value = serde_json::from_str(line).unwrap();
        for payload in [
            v["step_update"]["text_delta"].as_str(),
            v["result"]["response"].as_str(),
        ]
        .into_iter()
        .flatten()
        {
            // Single-letter deltas also occur in field names; match string values.
            assert!(!debug.contains(&format!("{payload:?}")), "payload leaked");
        }
    }
}
fn active_stream(role: Role, limits: Limits) -> Vec<(u64, Rule, Act)> {
    let mut p = parser();
    let mut m = monitor(role, limits);
    for s in send(&mut p, json!({"event":"init"}), 1000) {
        m.feed(s);
    }
    let user = json!({"event":"step_update","step_update":{"step_index":0,"step_type":"user_input","state":"DONE"}});
    for s in send(&mut p, user, 1500) {
        m.feed(s);
    }
    let mut decisions = Vec::new();
    for now in (10_000..=600_000).step_by(10_000).chain([601_000]) {
        if (90_000..=590_000).contains(&now) && now % 100_000 == 90_000 {
            for s in send(&mut p, step(now, "agent_response"), now) {
                m.feed(s);
            }
        }
        if let Some(d) = m.tick(
            now,
            Ctx {
                edited: false,
                safe_alt: true,
            },
        ) {
            decisions.push((now, d.rule, d.act));
        }
        if limits.milestone_s.is_none() {
            assert_eq!(m.state(), State::Active);
        }
    }
    decisions
}
#[test]
fn active_agy_run_passes_600s_with_no_m_warn_or_kill() {
    assert!(active_stream(
        Role::Implement,
        Limits {
            milestone_s: None,
            ..Limits::default()
        }
    )
    .is_empty());
}
#[test]
fn active_orchestrate_run_passes_600s_with_no_m_warn_or_kill() {
    assert!(active_stream(
        Role::Orchestrate,
        Limits {
            milestone_s: None,
            ..Limits::default()
        }
    )
    .is_empty());
}
#[test]
fn control_default_limits_warn_m_in_the_same_stream() {
    assert_eq!(
        active_stream(Role::Implement, Limits::default()),
        vec![
            (370_000, Rule::Milestone, Act::Warn),
            (601_000, Rule::Milestone, Act::Kill)
        ]
    );
}
