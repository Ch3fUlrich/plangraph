use super::*;
use std::cell::RefCell;

const HYDROGEN: &str = "agy-gemini-4-hydrogen-high";

fn leg(id: &str) -> Value {
    let harness = if id.starts_with("opencode") {
        "opencode"
    } else {
        "agy"
    };
    let model = id.strip_prefix("agy-").unwrap_or(id);
    json!({"id": id, "harness": harness, "model": model, "family": "gemini"})
}
fn plan(legs: &[&str], limit: usize, wait: f64) -> Value {
    json!({
        "first_leg": leg(legs[0]),
        "legs": legs.iter().map(|id| leg(id)).collect::<Vec<_>>(),
        "seat_admission": {"legs": {HYDROGEN: limit}, "queue_wait_seconds": wait, "poll_seconds": 5.0},
    })
}

/// A clock that only moves when admission sleeps; `on_sleep` is the rest of the fleet moving on.
struct Fake<'a> {
    clock: RefCell<f64>,
    routes: Vec<Value>,
    routed: usize,
    on_sleep: Box<dyn FnMut(f64) + 'a>,
}
impl<'a> Fake<'a> {
    fn new(routes: Vec<Value>, on_sleep: impl FnMut(f64) + 'a) -> Self {
        Self {
            clock: RefCell::new(1000.0),
            routes,
            routed: 0,
            on_sleep: Box::new(on_sleep),
        }
    }
}
impl Ports for Fake<'_> {
    fn route(&mut self) -> Result<Value, ToolError> {
        let plan = self.routes[self.routed.min(self.routes.len() - 1)].clone();
        self.routed += 1;
        Ok(plan)
    }
    fn now(&self) -> f64 {
        *self.clock.borrow()
    }
    fn sleep(&mut self, duration: Duration) {
        *self.clock.borrow_mut() += duration.as_secs_f64();
        (self.on_sleep)(*self.clock.borrow());
    }
}
trait Okay<T> {
    fn okay(self) -> T;
}
impl<T> Okay<T> for Result<T, ToolError> {
    fn okay(self) -> T {
        ok(self)
    }
}
fn ok<T>(result: Result<T, ToolError>) -> T {
    match result {
        Ok(value) => value,
        Err(error) => panic!("admission failed: {}", error.into_error_data().message),
    }
}
fn quiet() -> Fake<'static> {
    Fake::new(vec![], |_| {})
}
fn first(plan: &Value) -> &str {
    plan["first_leg"]["id"].as_str().unwrap()
}

#[test]
fn fifteen_requests_on_one_leg_start_three_and_queue_the_rest() {
    let state = tempfile::tempdir().unwrap();
    let mut started = Vec::new();
    let mut refused = 0;
    for _ in 0..15 {
        // No other leg has room and the wait bound is spent: the seat is not started on the leg.
        match admit(state.path(), plan(&[HYDROGEN], 3, 0.0), &mut quiet()) {
            Ok((p, slot)) => {
                assert_eq!(first(&p), HYDROGEN);
                started.push(slot.expect("a bounded leg holds a slot"));
            }
            Err(ToolError::Refused { message, .. }) => {
                assert!(message.starts_with("seat_queue_timeout:"), "{message}");
                refused += 1;
            }
            Err(_) => panic!("unexpected error kind"),
        }
    }
    assert_eq!((started.len(), refused), (3, 12));
}

#[test]
fn a_queued_seat_starts_when_a_slot_frees() {
    let state = tempfile::tempdir().unwrap();
    let held: RefCell<Vec<Reservation>> = RefCell::new(
        (0..2)
            .map(|_| {
                admit(state.path(), plan(&[HYDROGEN], 2, 600.0), &mut quiet())
                    .okay()
                    .1
                    .unwrap()
            })
            .collect(),
    );
    let mut waiting = Fake::new(vec![plan(&[HYDROGEN], 2, 600.0)], |now| {
        // One seat ends 12 seconds into the wait.
        if now >= 1010.0 {
            held.borrow_mut().pop();
        }
    });
    let (p, slot) = ok(admit(
        state.path(),
        plan(&[HYDROGEN], 2, 600.0),
        &mut waiting,
    ));
    assert_eq!(first(&p), HYDROGEN);
    assert!(slot.is_some());
    assert_eq!(waiting.routed, 2, "routed again on every poll");
}

#[test]
fn a_limit_hit_while_queued_stops_starts_and_takes_the_route_fallback() {
    let state = tempfile::tempdir().unwrap();
    let _full: Vec<_> = (0..2)
        .map(|_| {
            admit(state.path(), plan(&[HYDROGEN], 2, 600.0), &mut quiet())
                .okay()
                .1
        })
        .collect();
    // The next route no longer lists hydrogen: the leg booked a 429 and the route skips it.
    let weak = json!({"first_leg": leg("opencode-go-deepseek-v4-flash"), "legs": [leg("opencode-go-deepseek-v4-flash")]});
    let mut waiting = Fake::new(vec![weak], |_| {});
    let (p, slot) = ok(admit(
        state.path(),
        plan(&[HYDROGEN], 2, 600.0),
        &mut waiting,
    ));
    assert_eq!(first(&p), "opencode-go-deepseek-v4-flash");
    assert!(slot.is_none());
    assert_eq!(
        *waiting.clock.borrow(),
        1005.0,
        "one poll, not the whole wait bound"
    );
}

#[test]
fn past_the_wait_bound_the_seat_is_refused_not_moved_to_a_sibling_leg() {
    let state = tempfile::tempdir().unwrap();
    let _full = admit(state.path(), plan(&[HYDROGEN], 1, 600.0), &mut quiet())
        .okay()
        .1;
    let two = plan(&[HYDROGEN, "agy-gemini-4-argon-low"], 1, 20.0);
    let mut waiting = Fake::new(vec![two.clone()], |_| {});
    match admit(state.path(), two, &mut waiting) {
        Err(ToolError::Refused { message, .. }) => {
            assert!(message.starts_with("seat_queue_timeout:"), "{message}")
        }
        _ => panic!("a full leg past the wait bound must refuse"),
    }
}

#[test]
fn a_limit_that_leaves_only_sibling_legs_of_the_harness_is_refused() {
    let state = tempfile::tempdir().unwrap();
    let _full = admit(state.path(), plan(&[HYDROGEN], 1, 600.0), &mut quiet())
        .okay()
        .1;
    // Hydrogen booked a 429: the route now leads with a sibling agy leg, which is no fallback seat.
    let sibling = json!({"first_leg": leg("agy-gemini-4-neon-medium"), "legs": [leg("agy-gemini-4-neon-medium")]});
    let mut waiting = Fake::new(vec![sibling], |_| {});
    match admit(state.path(), plan(&[HYDROGEN], 1, 600.0), &mut waiting) {
        Err(ToolError::Refused { message, .. }) => {
            assert!(message.starts_with("seat_leg_limited:"), "{message}")
        }
        _ => panic!("a sibling of the same harness must refuse"),
    }
}

#[test]
fn the_agy_cli_selector_keeps_secondary_agy_legs_out_of_the_queue_fallback() {
    let state = tempfile::tempdir().unwrap();
    let _full = admit(state.path(), plan(&[HYDROGEN], 1, 600.0), &mut quiet())
        .okay()
        .1;
    // `leg: agy-cli` filters the route to the four agy legs (cli_review::select) before admission.
    let args: crate::placement::Args = serde_json::from_value(json!({"leg": "agy-cli"})).unwrap();
    let routed = json!({"first_leg": leg(HYDROGEN), "legs": [
        leg(HYDROGEN), leg("agy-gemini-4-neon-medium"), leg("agy-gemini-4-argon-low"),
        leg("opencode-go-deepseek-v4-flash")],
        "seat_admission": {"legs": {HYDROGEN: 1}, "queue_wait_seconds": 0.0}});
    let selected = crate::cli_review::select(&args, routed.to_string()).okay();
    let selected: Value = serde_json::from_str(&selected).unwrap();
    assert_eq!(selected["legs"].as_array().unwrap().len(), 3);
    match admit(state.path(), selected, &mut quiet()) {
        Err(ToolError::Refused { message, .. }) => {
            assert!(message.starts_with("seat_queue_timeout:"), "{message}")
        }
        _ => panic!("must refuse, not start a secondary agy leg"),
    }
}

#[test]
fn live_review_runs_count_and_a_reservation_is_not_counted_twice() {
    let state = tempfile::tempdir().unwrap();
    let write = |id: &str, status: &str, started: f64| {
        let dir = state.path().join("runs").join(id);
        std::fs::create_dir_all(&dir).unwrap();
        let record = json!({"run_id": id, "role": "review", "status": status, "started": started,
            "plan": {"first_leg": {"id": HYDROGEN}}});
        std::fs::write(dir.join("record.json"), record.to_string()).unwrap();
    };
    write("run-a", "running", 10.0);
    write("run-b", "completed", 11.0);
    write("run-c", "queued", 12.0);
    // Two live runs of three: one slot left, a second request is full.
    let (p, slot) = ok(admit(state.path(), plan(&[HYDROGEN], 3, 0.0), &mut quiet()));
    assert_eq!(first(&p), HYDROGEN);
    assert!(admit(state.path(), plan(&[HYDROGEN], 3, 0.0), &mut quiet()).is_err());
    // The reservation's run record appears: still three slots taken, not four.
    write("run-d", "running", 1000.5);
    let (count, _) = occupied(
        state.path(),
        &crate::records::list_runs(state.path()),
        HYDROGEN,
        1001.0,
    );
    assert_eq!(count, 3);
    drop(slot);
    let (count, _) = occupied(
        state.path(),
        &crate::records::list_runs(state.path()),
        HYDROGEN,
        1001.0,
    );
    assert_eq!(count, 3);
}

#[test]
fn a_leg_without_a_bound_and_a_plan_without_a_policy_pass_straight_through() {
    let state = tempfile::tempdir().unwrap();
    let weak = plan(&["opencode-go-glm-5.3-flash"], 1, 0.0);
    let (p, slot) = ok(admit(state.path(), weak, &mut quiet()));
    assert_eq!(first(&p), "opencode-go-glm-5.3-flash");
    assert!(slot.is_none());
    let bare = json!({"first_leg": leg(HYDROGEN), "legs": [leg(HYDROGEN)]});
    assert!(ok(admit(state.path(), bare, &mut quiet())).1.is_none());
}

#[test]
fn fifteen_seats_asking_at_once_never_start_more_than_the_bound() {
    let state = tempfile::tempdir().unwrap();
    let slots: std::sync::Mutex<Vec<Reservation>> = Default::default();
    std::thread::scope(|scope| {
        for _ in 0..15 {
            scope.spawn(|| {
                if let Ok((_, Some(slot))) =
                    admit(state.path(), plan(&[HYDROGEN], 3, 0.0), &mut quiet())
                {
                    slots.lock().unwrap().push(slot);
                }
            });
        }
    });
    assert_eq!(slots.lock().unwrap().len(), 3);
}
