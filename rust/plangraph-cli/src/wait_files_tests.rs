use super::*;
use std::cell::Cell;

#[test]
fn status_files_end_at_name_done_or_blocked() {
    assert_eq!(status_state("DONE plangraph:A abc\n"), FileState::Pending);
    assert_eq!(
        status_state("DONE plangraph:A abc\npr10 DONE\nHEAD: x\n"),
        FileState::Done
    );
    assert_eq!(status_state("BLOCKED review no leg\n"), FileState::Blocked);
}

fn files(statuses: &[PathBuf], results: &[PathBuf], deadline: u64) -> Outcome {
    let elapsed = Cell::new(0u64);
    wait_files(
        statuses,
        results,
        &mut |d| elapsed.set(elapsed.get() + d.as_secs()),
        &mut || Duration::from_secs(elapsed.get()),
        Duration::from_secs(deadline),
        Duration::from_secs(10),
    )
}

#[test]
fn files_wait_for_every_status_and_result_and_name_the_pending_one() {
    let dir = tempfile::tempdir().unwrap();
    let done = dir.path().join("a.status");
    let open = dir.path().join("b.status");
    let seat = dir.path().join("seat.result");
    std::fs::write(&done, "x DONE\n").unwrap();
    std::fs::write(&open, "DONE k s\n").unwrap();
    let Outcome::Timeout { detail } = files(
        &[done.clone(), open.clone()],
        std::slice::from_ref(&seat),
        30,
    ) else {
        panic!()
    };
    assert!(
        detail.contains("b.status")
            && detail.contains("seat.result")
            && !detail.contains("a.status"),
        "{detail}"
    );
    std::fs::write(&open, "b DONE\n").unwrap();
    std::fs::write(&seat, "{}").unwrap();
    assert_eq!(files(&[done.clone(), open], &[seat], 30).code(), 0);
    std::fs::write(&done, "BLOCKED review x\n").unwrap();
    assert_eq!(files(&[done], &[], 30).code(), 1);
}
