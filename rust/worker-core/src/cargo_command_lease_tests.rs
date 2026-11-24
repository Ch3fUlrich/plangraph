use super::*;
use crate::cargo_cache::{lease_holders, try_exclusive_lease};

fn script(dir: &Path, name: &str, body: &str) -> PathBuf {
    crate::executable_test_support::write_script(dir, name, &format!("#!/bin/sh\n{body}\n"))
}

#[test]
fn the_lease_is_held_during_the_command_and_free_between_commands() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    let slow = script(tmp.path(), "slow-cargo", "sleep 1");
    let quick = script(tmp.path(), "quick-cargo", "exit 0");
    assert!(try_exclusive_lease(&target).unwrap().is_some());
    let blocked_during = std::thread::scope(|scope| {
        let watcher = scope.spawn(|| {
            for _ in 0..200 {
                if try_exclusive_lease(&target).unwrap().is_none() {
                    return true;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            false
        });
        assert_eq!(
            run_command(Some(&target), None, &slow, &[], WAIT).unwrap(),
            0
        );
        watcher.join().unwrap()
    });
    assert!(blocked_during, "the lease was free during the command");
    assert_eq!(
        run_command(Some(&target), None, &quick, &[], WAIT).unwrap(),
        0
    );
    assert!(
        try_exclusive_lease(&target).unwrap().is_some(),
        "trim is possible between commands"
    );
    assert!(lease_holders(&target).is_empty());
}

#[test]
fn an_outer_lease_is_reused_and_the_exit_code_is_returned() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    let exclusive = try_exclusive_lease(&target).unwrap().unwrap();
    let ok = script(tmp.path(), "ok", "exit 3");
    // Without the marker the shared lease cannot be taken under an exclusive holder.
    assert!(run_command(Some(&target), None, &ok, &[], Duration::ZERO).is_err());
    // With it (preflight's artifact staging holds the exclusive lease), the command runs.
    let held = OsString::from(&target);
    assert_eq!(
        run_command(Some(&target), Some(&held), &ok, &[], Duration::ZERO).unwrap(),
        3
    );
    assert!(!outer_holds(&target, Some(OsStr::new("/elsewhere"))));
    drop(exclusive);
}

#[test]
fn the_child_marks_the_lease_as_held_for_nested_cargo() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    let out = tmp.path().join("env");
    let cargo = script(
        tmp.path(),
        "env-cargo",
        &format!("echo \"$PLANGRAPH_CARGO_LEASE_HELD\" > {}", out.display()),
    );
    run_command(Some(&target), None, &cargo, &[], WAIT).unwrap();
    assert_eq!(
        std::fs::read_to_string(out).unwrap().trim(),
        target.to_str().unwrap()
    );
}

#[test]
fn a_refused_lease_names_the_blocking_pid() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    let _trim = try_exclusive_lease(&target).unwrap().unwrap();
    let cargo = script(tmp.path(), "never", "exit 0");
    let error = run_command(Some(&target), None, &cargo, &[], Duration::ZERO).unwrap_err();
    assert!(
        error
            .to_string()
            .contains(&format!("held by pid {}", std::process::id())),
        "{error}"
    );
}

#[test]
fn the_real_cargo_search_skips_the_shim_itself() {
    let tmp = tempfile::tempdir().unwrap();
    let shim_dir = tmp.path().join("shim");
    let real_dir = tmp.path().join("real");
    std::fs::create_dir_all(&real_dir).unwrap();
    let exe = script(tmp.path(), "pg-spawnd", "exit 0");
    let link = install_shim(&shim_dir, &exe).unwrap();
    assert_eq!(install_shim(&shim_dir, &exe).unwrap(), link, "idempotent");
    let real = script(&real_dir, "cargo", "exit 0");
    let path = std::env::join_paths([&shim_dir, &real_dir]).unwrap();
    assert_eq!(real_cargo(&path, &exe), Some(real));
    assert_eq!(real_cargo(shim_dir.as_os_str(), &exe), None);
}

#[test]
fn a_child_whose_shim_already_exited_refuses_to_run_cargo() {
    // In `pre_exec` the child's parent is the shim; any other parent means the shim is gone.
    // SAFETY: getppid has no preconditions.
    let parent = unsafe { libc::getppid() };
    die_with_parent(parent).expect("the real parent passes");
    let error = die_with_parent(libc::pid_t::MAX).unwrap_err();
    assert!(error.to_string().contains("shim exited"), "{error}");
}
