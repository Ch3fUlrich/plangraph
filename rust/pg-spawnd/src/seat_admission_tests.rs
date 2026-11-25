use super::*;
use serde_json::json;
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
    let write = |id: &str, status: &str, started: f64, token: Option<&str>| {
        let dir = state.path().join("runs").join(id);
        std::fs::create_dir_all(&dir).unwrap();
        let record = json!({"run_id": id, "role": "review", "status": status, "started": started,
            "plan": {"first_leg": {"id": HYDROGEN}}, TOKEN_FIELD: token});
        std::fs::write(dir.join("record.json"), record.to_string()).unwrap();
    };
    write("run-a", "running", 10.0, None);
    write("run-b", "completed", 11.0, None);
    write("run-c", "queued", 12.0, None);
    // Two live runs of three: one slot left, a second request is full.
    let (p, slot) = ok(admit(state.path(), plan(&[HYDROGEN], 3, 0.0), &mut quiet()));
    assert_eq!(first(&p), HYDROGEN);
    assert!(admit(state.path(), plan(&[HYDROGEN], 3, 0.0), &mut quiet()).is_err());
    // The reservation's run record appears: still three slots taken, not four.
    let slot = slot.unwrap();
    write("run-d", "running", 1000.5, Some(&slot.token()));
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

/// A review run record naming the reservation it was admitted under.
fn record_for(state: &Path, id: &str, started: f64, token: Option<&str>) {
    let dir = state.join("runs").join(id);
    std::fs::create_dir_all(&dir).unwrap();
    let record = json!({"run_id": id, "role": "review", "status": "running", "started": started,
        "plan": {"first_leg": {"id": HYDROGEN}}, TOKEN_FIELD: token});
    std::fs::write(dir.join("record.json"), record.to_string()).unwrap();
}

#[test]
fn an_unrelated_run_started_just_before_a_reservation_does_not_release_it() {
    let state = tempfile::tempdir().unwrap();
    let mut clock = quiet();
    // Another seat's record appears 0.5 s before this reservation is written: it names no reservation.
    record_for(state.path(), "run-other", 999.5, None);
    let (_, slot) = ok(admit(state.path(), plan(&[HYDROGEN], 2, 0.0), &mut clock));
    let slot = slot.unwrap();
    // One live run and one held reservation: the leg is full at two, the reservation is still counted.
    assert!(admit(state.path(), plan(&[HYDROGEN], 2, 0.0), &mut clock).is_err());
    // Only the record that names the reservation takes it over.
    record_for(state.path(), "run-mine", 1000.2, Some(&slot.token()));
    let (count, finished) = occupied(
        state.path(),
        &crate::records::list_runs(state.path()),
        HYDROGEN,
        1001.0,
    );
    assert_eq!((count, finished.len()), (2, 1));
}

#[test]
fn a_stale_reservation_no_record_names_stops_counting_after_the_ttl() {
    let state = tempfile::tempdir().unwrap();
    let (_, slot) = ok(admit(state.path(), plan(&[HYDROGEN], 1, 0.0), &mut quiet()));
    let slot = slot.unwrap();
    let _ = &slot;
    let records = crate::records::list_runs(state.path());
    assert_eq!(occupied(state.path(), &records, HYDROGEN, 1100.0).0, 1);
    let (count, finished) = occupied(state.path(), &records, HYDROGEN, 1000.0 + 121.0);
    assert_eq!((count, finished.len()), (0, 1));
}

#[test]
fn a_route_with_no_leg_of_the_harness_left_while_queued_is_leg_limited() {
    struct NoLeg(f64);
    impl Ports for NoLeg {
        fn route(&mut self) -> Result<Value, ToolError> {
            Err(crate::placement::parameter(crate::cli_review::NO_LEG))
        }
        fn now(&self) -> f64 {
            self.0
        }
        fn sleep(&mut self, d: Duration) {
            self.0 += d.as_secs_f64();
        }
    }
    let state = tempfile::tempdir().unwrap();
    let _full = admit(state.path(), plan(&[HYDROGEN], 1, 600.0), &mut quiet())
        .okay()
        .1;
    match admit(
        state.path(),
        plan(&[HYDROGEN], 1, 600.0),
        &mut NoLeg(1000.0),
    ) {
        Err(ToolError::Refused { message, .. }) => {
            assert!(message.starts_with("seat_leg_limited:"), "{message}")
        }
        _ => panic!("an emptied harness route must refuse as leg limited"),
    }
}

/// Each group of a split review needs its own slot, so a split larger than the bound is capped too.
#[test]
fn a_split_larger_than_the_bound_never_holds_more_slots_than_the_leg_allows() {
    let state = tempfile::tempdir().unwrap();
    let bound = plan(&[HYDROGEN], 2, 0.0);
    // The whole review was admitted once: its token rides on the args and covers group 1.
    let (_, first) = ok(admit(
        state.path(),
        bound.clone(),
        &mut Fixed(bound.clone()),
    ));
    let first = first.unwrap();
    let mut args = crate::placement::Args::new();
    args.insert(TOKEN_FIELD.into(), json!(first.token()));
    let mut held = Vec::new();
    let mut tokens = Vec::new();
    let mut refused = 0;
    for index in 1..=5 {
        match crate::review_split::admit_group(
            state.path(),
            &bound,
            index,
            false,
            &args,
            &mut held,
            &mut Fixed(bound.clone()),
        ) {
            Ok(group) => tokens.push(group[TOKEN_FIELD].as_str().unwrap().to_owned()),
            Err(ToolError::Refused { message, .. }) => {
                assert!(message.starts_with("seat_queue_timeout:"), "{message}");
                refused += 1;
            }
            Err(_) => panic!("unexpected error"),
        }
    }
    // Group 1 rides the review's slot, group 2 takes the second; groups 3-5 wait for a free slot.
    assert_eq!((tokens.len(), refused, held.len()), (2, 3, 1));
    assert_eq!(tokens[0], first.token());
    assert_ne!(tokens[1], first.token());
    // A dry split never admits.
    let none = crate::review_split::admit_group(
        state.path(),
        &bound,
        4,
        true,
        &args,
        &mut held,
        &mut Fixed(bound.clone()),
    )
    .ok()
    .unwrap();
    assert!(none
        .get(TOKEN_FIELD)
        .is_some_and(|t| *t == json!(first.token())));
}

/// A group that waits routes again: when the route stops serving its leg it is refused at once, and the
/// groups already started stay in a failed seat group that names their run ids.
#[test]
fn a_queued_group_whose_leg_left_the_route_is_refused_and_started_runs_are_kept() {
    let state = tempfile::tempdir().unwrap();
    let bound = plan(&[HYDROGEN], 1, 600.0);
    let first = ok(admit(state.path(), bound.clone(), &mut quiet()))
        .1
        .unwrap();
    let mut args = crate::placement::Args::new();
    args.insert(TOKEN_FIELD.into(), json!(first.token()));
    // The route no longer serves hydrogen while group 2 waits for the one slot.
    let moved = json!({"first_leg": leg("opencode-go-deepseek-v4-flash"),
        "legs": [leg("opencode-go-deepseek-v4-flash")]});
    let mut ports = Fake::new(vec![moved], |_| {});
    let mut held = Vec::new();
    let error = match crate::review_split::admit_group(
        state.path(),
        &bound,
        2,
        false,
        &args,
        &mut held,
        &mut ports,
    ) {
        Err(error) => error,
        Ok(_) => panic!("the group must not run on another leg"),
    };
    assert!(held.is_empty());
    assert_eq!(
        *ports.clock.borrow(),
        1005.0,
        "refused at the first poll, not after the wait"
    );
    // The started group's run is kept in a failed seat group.
    let record = |id: &str| {
        let dir = state.path().join("runs").join(id);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("record.json"),
            json!({"run_id": id, "role": "review", "status": "running", "started": 1.0,
                "plan": {"first_leg": {"id": HYDROGEN}}})
            .to_string(),
        )
        .unwrap();
    };
    record("run-aaaa1111");
    let answer = crate::review_split::unstarted_groups(
        state.path(),
        "seats-abc123",
        &["run-aaaa1111".to_owned()],
        3,
        error,
    )
    .ok()
    .unwrap();
    assert_eq!(answer["status"], "failed");
    assert_eq!(answer["passed"], false);
    assert_eq!(answer["verdict"], "FAIL");
    assert_eq!(answer["unstarted_groups"], 2);
    assert_eq!(answer["seats"][0]["run_id"], "run-aaaa1111");
    assert!(answer["reason"]
        .as_str()
        .unwrap()
        .starts_with("seat_leg_limited:"));
    let saved =
        std::fs::read_to_string(state.path().join("seat-groups/seats-abc123.json")).unwrap();
    assert!(saved.contains("run-aaaa1111"), "{saved}");
}

/// The wait budget is a deadline: the last sleep is the time left, and a slot that frees at the
/// deadline is not taken by a seat that has used its budget.
#[test]
fn a_seat_past_its_wait_budget_never_takes_a_slot_that_just_freed() {
    let state = tempfile::tempdir().unwrap();
    let held: RefCell<Vec<Reservation>> = RefCell::new(vec![ok(admit(
        state.path(),
        plan(&[HYDROGEN], 1, 600.0),
        &mut quiet(),
    ))
    .1
    .unwrap()]);
    let slept = RefCell::new(Vec::new());
    let mut waiting = Fake::new(vec![plan(&[HYDROGEN], 1, 7.0)], |now| {
        slept.borrow_mut().push(now);
        // The slot frees exactly when the 7 s budget is spent.
        if now >= 1007.0 {
            held.borrow_mut().clear();
        }
    });
    match admit(state.path(), plan(&[HYDROGEN], 1, 7.0), &mut waiting) {
        Err(ToolError::Refused { message, .. }) => {
            assert!(message.starts_with("seat_queue_timeout:"), "{message}")
        }
        _ => panic!("a spent budget must refuse even when a slot is free"),
    }
    // Polls of 5 s, then the 2 s left: the clock never passes the deadline.
    assert_eq!(*slept.borrow(), [1005.0, 1007.0]);
}

/// A run on a quota retry occupies the leg of its running attempt, not its first leg.
#[test]
fn a_retried_run_counts_on_the_leg_it_runs_on() {
    let state = tempfile::tempdir().unwrap();
    let dir = state.path().join("runs/run-r1");
    std::fs::create_dir_all(&dir).unwrap();
    let record = json!({"run_id": "run-r1", "role": "review", "status": "running", "started": 5.0,
        "plan": {"first_leg": {"id": "opencode-go-deepseek-v4-flash"}},
        "attempts": [{"leg": {"id": "opencode-go-deepseek-v4-flash"}, "status": "failed"},
            {"leg": {"id": HYDROGEN}, "status": "running"}]});
    std::fs::write(dir.join("record.json"), record.to_string()).unwrap();
    let records = crate::records::list_runs(state.path());
    assert_eq!(occupied(state.path(), &records, HYDROGEN, 10.0).0, 1);
    assert_eq!(
        occupied(
            state.path(),
            &records,
            "opencode-go-deepseek-v4-flash",
            10.0
        )
        .0,
        0
    );
    // A reserve-now for the leg is refused while the retried run holds the only slot.
    let policy = json!({"legs": {HYDROGEN: 1}, "queue_wait_seconds": 900.0});
    match reserve_now(state.path(), &leg(HYDROGEN), &policy) {
        Err(ToolError::Refused { message, .. }) => {
            assert!(message.starts_with("seat_queue_timeout:"), "{message}")
        }
        _ => panic!("a retry into a saturated leg must be refused without waiting"),
    }
    assert!(reserve_now(
        state.path(),
        &leg(HYDROGEN),
        &json!({"legs": {HYDROGEN: 2}})
    )
    .ok()
    .unwrap()
    .is_some());
}

/// The budget is read after the lock and the occupancy scan: a scan that takes the seat past its budget
/// does not take the slot.
#[test]
fn a_slow_occupancy_scan_that_passes_the_budget_does_not_take_the_slot() {
    use std::cell::Cell;
    struct Slow {
        mono: Cell<f64>,
        armed: Cell<bool>,
    }
    impl Ports for Slow {
        fn route(&mut self) -> Result<Value, ToolError> {
            Ok(short_polls())
        }
        fn now(&self) -> f64 {
            // The scan after the first sleep takes 1.1 s of monotonic time.
            if self.armed.replace(false) {
                self.mono.set(self.mono.get() + 1.1);
            }
            1000.0
        }
        fn monotonic(&self) -> f64 {
            self.mono.get()
        }
        fn sleep(&mut self, d: Duration) {
            self.mono.set(self.mono.get() + d.as_secs_f64());
            self.armed.set(true);
        }
    }
    let state = tempfile::tempdir().unwrap();
    // One seat fills the leg; the second seat queues with a 1 s budget and the slot frees at its first poll.
    let held = RefCell::new(vec![ok(admit(
        state.path(),
        plan(&[HYDROGEN], 1, 1.0),
        &mut quiet(),
    ))
    .1
    .unwrap()]);
    struct Freeing<'a>(Slow, &'a RefCell<Vec<Reservation>>);
    impl Ports for Freeing<'_> {
        fn route(&mut self) -> Result<Value, ToolError> {
            self.0.route()
        }
        fn now(&self) -> f64 {
            self.0.now()
        }
        fn monotonic(&self) -> f64 {
            self.0.monotonic()
        }
        fn sleep(&mut self, d: Duration) {
            self.0.sleep(d);
            self.1.borrow_mut().clear();
        }
    }
    let mut ports = Freeing(
        Slow {
            mono: Cell::new(0.0),
            armed: Cell::new(false),
        },
        &held,
    );
    match admit(state.path(), short_polls(), &mut ports) {
        Err(ToolError::Refused { message, .. }) => {
            assert!(message.starts_with("seat_queue_timeout:"), "{message}")
        }
        _ => panic!("the slot must not be taken after the budget passed"),
    }
}

/// A 1 s budget polled every half second.
fn short_polls() -> Value {
    let mut bounded = plan(&[HYDROGEN], 1, 1.0);
    bounded["seat_admission"]["poll_seconds"] = json!(0.5);
    bounded
}

/// A route answer that leaves the leg uncapped after the budget is spent is not an admission.
#[test]
fn a_route_answer_after_the_budget_is_spent_does_not_admit_the_expired_seat() {
    use std::cell::Cell;
    struct Timed {
        mono: Cell<f64>,
        route_takes: f64,
    }
    impl Ports for Timed {
        fn route(&mut self) -> Result<Value, ToolError> {
            self.mono.set(self.mono.get() + self.route_takes);
            // The route moved to a leg with no bound.
            Ok(json!({"first_leg": leg("opencode-go-deepseek-v4-flash"),
                "legs": [leg("opencode-go-deepseek-v4-flash")]}))
        }
        fn now(&self) -> f64 {
            1000.0
        }
        fn monotonic(&self) -> f64 {
            self.mono.get()
        }
        fn sleep(&mut self, d: Duration) {
            self.mono.set(self.mono.get() + d.as_secs_f64());
        }
    }
    let refused_for = |wait: f64, poll: f64, route_takes: f64| {
        let state = tempfile::tempdir().unwrap();
        let mut full = plan(&[HYDROGEN], 1, wait);
        full["seat_admission"]["poll_seconds"] = json!(poll);
        let _held = ok(admit(state.path(), full.clone(), &mut quiet())).1;
        let mut ports = Timed {
            mono: Cell::new(0.0),
            route_takes,
        };
        match admit(state.path(), full, &mut ports) {
            Err(ToolError::Refused { message, .. }) => message,
            Ok((p, _)) => format!("admitted on {}", first(&p)),
            Err(_) => "error".into(),
        }
    };
    // The last sleep is the whole budget: the route is not asked, the seat is refused.
    assert!(refused_for(1.0, 5.0, 0.0).starts_with("seat_queue_timeout:"));
    // The route call itself spends the budget.
    assert!(refused_for(3.0, 0.5, 3.0).starts_with("seat_queue_timeout:"));
    // Within budget the route's uncapped leg is the fallback and is taken.
    assert_eq!(
        refused_for(3.0, 0.5, 0.5),
        "admitted on opencode-go-deepseek-v4-flash"
    );
}
