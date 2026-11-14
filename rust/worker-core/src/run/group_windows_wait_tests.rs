use std::{
    io::{BufRead, Write},
    os::windows::{io::AsRawHandle, io::FromRawHandle, io::OwnedHandle, process::CommandExt},
    process::{Child, Command},
    thread,
    time::{Duration, Instant},
};
use windows_sys::Win32::{Foundation::*, System::Threading::*};

pub(super) fn spawn_tree() -> ! {
    let child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "run::group_windows_tests::fake_child",
            "--nocapture",
        ])
        .env("PG_FAKE_CHILD", "tree-leaf")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        // spawn_grouped runs our parent without a console. Creation flags are
        // not inherited: suppress the leaf's console too, so Windows cannot
        // add a console host to this test's otherwise two-process job.
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .unwrap();
    // Creation has completed, including inherited job membership. launch() can
    // return before this helper's test harness has even reached fake_child().
    println!("tree-ready={}", child.id());
    std::io::stdout().flush().unwrap();
    // Neither process may time out naturally and mask a failed group stop.
    loop {
        thread::park();
    }
}

pub(super) fn tree_ready(child: &mut Child) -> (u32, thread::JoinHandle<()>) {
    let stdout = child.stdout.take().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    let output = thread::spawn(move || {
        for line in std::io::BufReader::new(stdout).lines() {
            let line = line.unwrap();
            if let Some((_, pid)) = line.split_once("tree-ready=") {
                tx.send(pid.parse::<u32>().unwrap()).unwrap();
            }
        }
    });
    // Only a hang guard: membership is asserted once, after the spawn signal.
    let pid = rx
        .recv_timeout(Duration::from_secs(30))
        .expect("helper did not signal grandchild creation");
    (pid, output)
}

fn image_name(pid: u32) -> std::io::Result<String> {
    // SAFETY: numeric pid and query-only access; successful handle is owned below.
    let raw = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if raw.is_null() {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: OpenProcess transferred a unique handle, closed by RAII.
    let process = unsafe { OwnedHandle::from_raw_handle(raw) };
    let mut path = vec![0u16; 32768];
    let mut size = path.len() as u32;
    // SAFETY: open query handle and writable UTF-16 buffer of size code units.
    if unsafe {
        QueryFullProcessImageNameW(process.as_raw_handle(), 0, path.as_mut_ptr(), &mut size)
    } == 0
    {
        return Err(std::io::Error::last_os_error());
    }
    Ok(String::from_utf16_lossy(&path[..size as usize]))
}

pub(super) fn member_names(pids: &[u32]) -> String {
    pids.iter()
        .map(|&pid| match image_name(pid) {
            Ok(image) => format!("{pid}: {image}"),
            Err(error) => format!("{pid}: <image query failed: {error}>"),
        })
        .collect::<Vec<_>>()
        .join(", ")
}

pub(super) fn assert_tree_members(pids: &[u32], child: u32, grandchild: u32) -> String {
    let names = member_names(pids);
    assert!(
        pids.len() == 2 && pids.contains(&child) && pids.contains(&grandchild),
        "expected only child {child} and grandchild {grandchild}; job members: {names}"
    );
    names
}

pub(super) fn assert_stop_requires_kill(group: &dyn super::RunGroup, pids: &[u32], names: &str) {
    use super::{stop_group, Identity, RunGroup, StopOutcome};
    struct LeaveAlive<'a>(&'a dyn RunGroup);
    impl RunGroup for LeaveAlive<'_> {
        fn identity(&self) -> &Identity {
            self.0.identity()
        }
        fn members(&self) -> std::io::Result<Vec<u32>> {
            self.0.members()
        }
        fn terminate(&self) {}
        fn kill(&self) {}
    }
    // Failing-first variant: suppress both stop signals against the same real
    // job. Requiring Empty here would fail while its descendant survives.
    let StopOutcome::Survivors(survivors) =
        stop_group(&LeaveAlive(group), Duration::ZERO, Duration::ZERO)
    else {
        panic!("a stop that leaves the job alive must detect survivors; job members: {names}");
    };
    assert!(
        survivors.len() == pids.len() && pids.iter().all(|pid| survivors.contains(pid)),
        "recorded job members: {names}; survivors: {}",
        member_names(&survivors)
    );
    assert!(
        pids.iter().all(|&pid| !super::gone(pid)),
        "job members: {names}"
    );
}

pub(super) fn until_deadline(deadline: Instant, mut predicate: impl FnMut() -> bool) {
    loop {
        if predicate() {
            break;
        }
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn ready_condition_wins_over_elapsed_poll_deadline() {
    // A descheduled observer must still accept an already satisfied condition.
    until_deadline(Instant::now() - Duration::from_secs(1), || true);
}
