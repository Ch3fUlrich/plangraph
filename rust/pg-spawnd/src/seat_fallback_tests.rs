use super::*;
use crate::seat_admission::{admit, is_queue_refusal, Ports};
use std::time::Duration;

const HYDROGEN: &str = "agy-gemini-4-hydrogen-high";

fn leg(id: &str) -> Value {
    json!({"id": id, "harness": if id.starts_with("opencode") {"opencode"} else {"agy"},
        "model": id, "family": "x"})
}
fn route(ids: &[&str]) -> Value {
    json!({"first_leg": leg(ids[0]), "legs": ids.iter().map(|i| leg(i)).collect::<Vec<_>>(),
        "seat_admission": {"legs": {HYDROGEN: 1}, "queue_wait_seconds": 600.0, "poll_seconds": 5.0}})
}

/// Admission ports whose every route answer is the one given.
struct Routes(Result<Value, String>, f64);
impl Ports for Routes {
    fn route(&mut self) -> Result<Value, ToolError> {
        self.0.clone().map_err(crate::placement::parameter)
    }
    fn now(&self) -> f64 {
        self.1
    }
    fn sleep(&mut self, d: Duration) {
        self.1 += d.as_secs_f64();
    }
}

#[test]
fn both_weak_seats_are_planned_each_pinned_to_its_leg() {
    let plans = seat_plans(&route(&[HYDROGEN, LEGS[1], "x", LEGS[0]]))
        .ok()
        .unwrap();
    let firsts: Vec<_> = plans
        .iter()
        .map(|p| p["first_leg"]["id"].as_str().unwrap())
        .collect();
    assert_eq!(firsts, LEGS);
    assert!(plans
        .iter()
        .all(|p| p["legs"].as_array().unwrap().len() == 1));
}

#[test]
fn a_missing_weak_seat_starts_none() {
    let mut started = 0;
    let result = start_seats(&route(&[HYDROGEN, LEGS[0]]), |_| {
        started += 1;
        Ok(json!({"run_id": "run-1"}))
    });
    match result {
        Err(ToolError::Refused { message, .. }) => {
            assert!(
                message.starts_with("seat_fallback_unavailable:"),
                "{message}"
            );
            assert!(message.contains(LEGS[1]));
        }
        _ => panic!("one weak seat alone is not the fallback"),
    }
    assert_eq!(started, 0);
}

/// The launch caller: a full smart leg refuses the queued seat, and the refusal starts both weak seats.
#[test]
fn a_refused_queue_starts_deepseek_and_glm_seats() {
    let state = tempfile::tempdir().unwrap();
    let _held = admit(
        state.path(),
        route(&[HYDROGEN]),
        &mut Routes(Ok(route(&[HYDROGEN])), 0.0),
    )
    .ok()
    .unwrap()
    .1;
    // The leg booked a 429 while the seat queued; with `leg: agy-cli` no agy leg is left to route.
    let mut ports = Routes(Err(crate::cli_review::NO_LEG.into()), 0.0);
    let error = match admit(state.path(), route(&[HYDROGEN]), &mut ports) {
        Err(error) => error,
        Ok(_) => panic!("a full leg must not admit"),
    };
    assert!(is_queue_refusal(&error));
    match &error {
        ToolError::Refused { message, .. } => {
            assert!(message.starts_with("seat_leg_limited:"), "{message}")
        }
        _ => unreachable!(),
    }
    let mut ids = 0;
    let mut legs = Vec::new();
    let started = start_seats(&route(&[LEGS[0], LEGS[1], HYDROGEN]), |plan| {
        ids += 1;
        legs.push(plan["first_leg"]["id"].as_str().unwrap().to_owned());
        Ok(json!({"run_id": format!("run-{ids}")}))
    })
    .ok()
    .unwrap();
    assert!(started.failure.is_none());
    assert_eq!(started.ids, ["run-1", "run-2"]);
    assert_eq!(legs, LEGS);
}

#[test]
fn a_timeout_or_other_refusal_is_told_apart() {
    let timeout = ToolError::Refused {
        kind: RefusalKind::Spawn,
        message: "seat_queue_timeout: x".into(),
        legs: vec![],
    };
    assert!(is_queue_refusal(&timeout));
    assert!(!is_queue_refusal(&ToolError::Internal(
        "seat_queue_timeout:".into()
    )));
    assert!(!is_queue_refusal(&refused("seat_admission_busy: x".into())));
}

#[test]
fn the_fallback_request_drops_the_selector_and_any_reservation_claim() {
    let args: Args = serde_json::from_value(
        json!({"leg": "agy-cli", "seat_reservation": "forged", "role": "review"}),
    )
    .unwrap();
    let open = seat_args(&args);
    assert!(open.get("leg").is_none() && open.get("seat_reservation").is_none());
    assert_eq!(open["role"], "review");
}

#[test]
fn a_seat_that_split_answers_with_every_run_it_started() {
    let mut n = 0;
    let started = start_seats(&route(&[LEGS[0], LEGS[1]]), |_| {
        n += 1;
        Ok(if n == 1 {
            json!({"seat_group": "seats-ab", "seats": [{"run_id": "run-a1"}, {"run_id": "run-a2"}]})
        } else {
            json!({"run_id": "run-b"})
        })
    })
    .ok()
    .unwrap();
    assert!(started.failure.is_none());
    assert_eq!(started.ids, ["run-a1", "run-a2", "run-b"]);
}

#[test]
fn a_second_launch_that_fails_keeps_the_run_that_started() {
    let mut n = 0;
    let started = start_seats(&route(&[LEGS[0], LEGS[1]]), |_| {
        n += 1;
        if n == 1 {
            Ok(json!({"run_id": "run-1"}))
        } else {
            Err(ToolError::Internal("launch failed".into()))
        }
    })
    .ok()
    .unwrap();
    assert_eq!(started.ids, ["run-1"]);
    assert!(started.failure.is_some());
    // An answer with no run id is a failure too, after what already started.
    let mut n = 0;
    let started = start_seats(&route(&[LEGS[0], LEGS[1]]), |_| {
        n += 1;
        Ok(if n == 1 {
            json!({"run_id": "run-1"})
        } else {
            json!({"status": "refused"})
        })
    })
    .ok()
    .unwrap();
    assert_eq!((started.ids.len(), started.failure.is_some()), (1, true));
}
