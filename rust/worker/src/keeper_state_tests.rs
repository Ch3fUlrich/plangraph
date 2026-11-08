#[test]
fn live_states_include_waiting_and_exclude_agent_lease_and_terminal_states() {
    assert_eq!(
        super::WORKER_LIVE_STATES,
        &["open", "ready", "running", "waiting", "needs_input"]
    );
    // The projector writes `waiting` (usage_limit/outage attempt ends and
    // TIMER_STARTED), so a park or ask after a timer must accept it.
    assert!(super::WORKER_LIVE_STATES.contains(&"waiting"));
    for state in ["claimed", "parked", "failed", "done", "cancelled"] {
        assert!(!super::WORKER_LIVE_STATES.contains(&state));
    }
}

/// EXEC-OBSERVED-STATE: the state the run observed is what park and ask
/// send; the live set stands only when there was no usable observation.
#[test]
fn expected_state_is_the_observation_or_the_live_set_fallback() {
    let live = || {
        super::WORKER_LIVE_STATES
            .iter()
            .map(|s| (*s).to_owned())
            .collect::<Vec<_>>()
    };
    // A projector-written `waiting` the run read is the CAS it presents,
    // so the wait-cap park after usage_limit/outage still lands.
    assert_eq!(
        super::expected_state_for(Some("waiting".to_owned()), Some(live())),
        Some(vec!["waiting".to_owned()])
    );
    // `claimed` names a claim, not a settable state, and a terminal state
    // would only swap one refusal for another: both keep the fallback.
    for observed in ["claimed", "parked", "failed", "done", "cancelled"] {
        assert_eq!(
            super::expected_state_for(Some(observed.to_owned()), Some(live())),
            Some(live()),
            "{observed} is not an observation the keeper accepts"
        );
    }
    assert_eq!(super::expected_state_for(None, Some(live())), Some(live()));
    assert_eq!(super::expected_state_for(None, None), None);
}
