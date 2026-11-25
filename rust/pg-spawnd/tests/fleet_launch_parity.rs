//! Black-box fixtures for the fleet launcher CLI.
#[cfg(target_os = "linux")]
#[path = "../../test_support/python.rs"]
mod test_python;

#[cfg(target_os = "linux")]
#[path = "fleet_launch/dry_run.rs"]
mod dry_run;
#[cfg(target_os = "linux")]
#[path = "fleet_launch/interrupted.rs"]
mod interrupted;
#[cfg(target_os = "linux")]
#[path = "fleet_launch/peers.rs"]
mod peers;
#[cfg(target_os = "linux")]
#[path = "fleet_launch/records.rs"]
mod records;
#[cfg(target_os = "linux")]
#[path = "fleet_launch/refusals.rs"]
mod refusals;
#[cfg(target_os = "linux")]
#[path = "fleet_launch/remote.rs"]
mod remote;
#[cfg(target_os = "linux")]
#[path = "fleet_launch/restart.rs"]
mod restart;
#[cfg(target_os = "linux")]
#[path = "fleet_launch/resume.rs"]
mod resume;
#[cfg(target_os = "linux")]
#[path = "fleet_launch/sandbox.rs"]
mod sandbox;
#[cfg(target_os = "linux")]
mod spawn_attempt_delivery;
#[cfg(target_os = "linux")]
#[path = "fleet_launch/watchdog.rs"]
mod watchdog;

#[cfg(target_os = "linux")]
fn main() {
    use std::env;
    use std::path::Path;

    let mut args = env::args();
    let invocation = args.next().unwrap();
    let name = Path::new(&invocation)
        .file_name()
        .unwrap()
        .to_str()
        .unwrap();
    if peers::invoke(name, args.collect()) {
        return;
    }
    use sandbox::Launcher;
    let rust = Launcher::Rust(None);
    check(&rust);
    check_exec_entry_point();
    records::check_lifecycle(&rust);
    interrupted::check(&rust);
    interrupted::check_counter(&rust, false);
    interrupted::check_counter(&rust, true);
    watchdog::check_launcher_contract(&rust);
    println!("fleet-launch S2 fixtures passed: interruption, ledger, shared Python/Rust counter and watchdog launch contract");

    match Launcher::find() {
        Some(Launcher::Pinned(checkout)) => {
            let python = Launcher::Pinned(checkout.clone());
            check(&python);
            check(&Launcher::Rust(Some(checkout.clone())));
            interrupted::check(&python);
            interrupted::check_counter(&python, false);
            watchdog::check_launcher_contract(&python);
            watchdog::check(&checkout);
            println!("fleet-launch pinned Python parity passed, including watchdog recovery");
        }
        Some(Launcher::Explicit(path)) => {
            check(&Launcher::Explicit(path));
            println!("fleet-launch replacement fixtures passed");
        }
        _ => {}
    }
}

/// The operator's `fleet-launch` entry point as a one-line exec of `pg-spawnd session`: worker
/// respawns through it resume the saved session (dry-run line with `--resume`/`-n`) and
/// refuse with the named reasons, exactly as `pg-spawnd session` does.
#[cfg(target_os = "linux")]
fn check_exec_entry_point() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let entry = dir.path().join("fleet-launch");
    std::fs::write(
        &entry,
        format!(
            "#!/bin/sh\nexec '{}' session \"$@\"\n",
            env!("CARGO_BIN_EXE_pg-spawnd")
        ),
    )
    .unwrap();
    std::fs::set_permissions(&entry, std::fs::Permissions::from_mode(0o755)).unwrap();
    resume::check(&sandbox::Launcher::Explicit(entry));
    println!("fleet-launch exec entry point passed: worker respawn resumes the saved session");
}

#[cfg(target_os = "linux")]
fn check(launcher: &sandbox::Launcher) {
    dry_run::check(launcher);
    refusals::check(launcher);
    records::check(launcher);
    records::check_id_resolution(launcher);
    remote::check(launcher);
    if matches!(launcher, sandbox::Launcher::Rust(_)) {
        restart::check(launcher);
        resume::check(launcher);
        records::check_router_prompt(launcher);
        records::check_batch_prompt(launcher);
    }
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("fleet-launch parity skipped: requires Linux");
}
