use super::*;

/// The packet the park-path tests park.
pub(super) fn g3_packet() -> ParkPacket {
    ParkPacket {
        card: "c-1".into(),
        tier: Tier::Free,
        exit_class: pg_worker_core::exit_class::ExitClass::UsageLimit,
        check_tail: "tail".into(),
    }
}

/// An execution identity built the way `park`/`ask_question` build one from
/// their input: attempt from the input, `expected_state` from the input.
pub(super) fn g3_exec(expected_state: Option<Vec<String>>) -> ExecId {
    ExecId {
        workflow_id: Some("wf-1".into()),
        run_id: Some("run-1".into()),
        attempt: Some(4),
        expected_state,
    }
}

pub(super) fn g3_live() -> Vec<String> {
    crate::keeper::WORKER_LIVE_STATES
        .iter()
        .map(|s| (*s).to_owned())
        .collect()
}
