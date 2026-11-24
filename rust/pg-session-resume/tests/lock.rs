//! Own test binary: another test forking a child while a lock fd is open would hold the
//! lock for the instant between fork and exec and fail these by accident.

use pg_session_resume::lock::Lock;

#[test]
fn second_tick_finds_lock_held() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("lock");
    let first = Lock::acquire(&path).expect("free lock");
    assert!(Lock::acquire(&path).is_none(), "held by this live process");
    drop(first);
    assert!(Lock::acquire(&path).is_some(), "released by the owner");
    // an owner that died leaves a file but no kernel lock
    std::fs::write(&path, "4294967290").unwrap();
    assert!(Lock::acquire(&path).is_some());
}

#[test]
fn simultaneous_ticks_never_both_get_the_lock() {
    let dir = tempfile::tempdir().unwrap();
    let path = std::sync::Arc::new(dir.path().join("lock"));
    let start = std::sync::Arc::new(std::sync::Barrier::new(8));
    let done = std::sync::Arc::new(std::sync::Barrier::new(8));
    let threads: Vec<_> = (0..8)
        .map(|_| {
            let (path, start, done) = (path.clone(), start.clone(), done.clone());
            std::thread::spawn(move || {
                start.wait();
                let got = Lock::acquire(&path);
                done.wait(); // everyone has tried while the winner still holds it
                got.is_some()
            })
        })
        .collect();
    let winners = threads
        .into_iter()
        .map(|t| t.join().unwrap())
        .filter(|w| *w)
        .count();
    assert_eq!(winners, 1);
}
