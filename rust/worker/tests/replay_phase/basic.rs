use super::{history, replayer};

#[tokio::test]
async fn replays_phase_two_cards() {
    replayer()
        .replay_workflow(history("phase_two_cards"))
        .await
        .expect("phase_two_cards history replays");
}

#[tokio::test]
async fn replays_phase_defer() {
    replayer()
        .replay_workflow(history("phase_defer"))
        .await
        .expect("phase_defer history replays");
}
