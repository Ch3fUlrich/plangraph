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
#[path = "fleet_launch/parent.rs"]
mod parent;
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
    records::check_lifecycle(&rust);
    parent::parent_session_is_recorded(&rust);
    println!("fleet-launch fixture parent_session_is_recorded passed");
    parent::invalid_parent_is_not_recorded(&rust);
    println!("fleet-launch fixture invalid_parent_is_not_recorded passed");
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

#[cfg(target_os = "linux")]
fn check(launcher: &sandbox::Launcher) {
    dry_run::check(launcher);
    refusals::check(launcher);
    records::check(launcher);
    records::check_id_resolution(launcher);
    remote::check(launcher);
    if matches!(launcher, sandbox::Launcher::Rust(_)) {
        restart::check(launcher);
        records::check_router_prompt(launcher);
        records::check_batch_prompt(launcher);
    }
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("fleet-launch parity skipped: requires Linux");
}
