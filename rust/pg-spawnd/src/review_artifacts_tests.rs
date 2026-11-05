use super::*;
#[test]
fn full_text_not_tail_and_unknown_format_nonpassing() {
    let state = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let dir = state.join("runs/run-test");
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join("live.log");
    let record = json!({"run_id":"run-test","live_log":path,"stdout_tail":"VERDICT: ACCEPT"});
    let full = format!("{}\nVERDICT: ACCEPT", "detail ".repeat(10000));
    fs::write(
        &path,
        format!(
            "{}\n",
            json!({"type":"result","is_error":false,"result":full})
        ),
    )
    .unwrap();
    assert_eq!(review_text(&state, &record).unwrap(), full);
    fs::write(&path, "unknown\n").unwrap();
    assert!(review_text(&state, &record)
        .unwrap()
        .contains("VERDICT: HALT"));
    fs::remove_dir_all(state).unwrap();
}
#[test]
fn traversal_run_id_refused() {
    assert!(write_review_artifacts(
        Path::new("unused"),
        &json!({"run_id":"../escape"}),
        "VERDICT: ACCEPT"
    )
    .is_err());
}
