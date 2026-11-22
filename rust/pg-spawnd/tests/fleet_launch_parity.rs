//! Black-box fixtures for the fleet launcher CLI.
#[cfg(target_os = "linux")]
#[path = "fleet_launch/dry_run.rs"]
mod dry_run;
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
#[path = "fleet_launch/sandbox.rs"]
mod sandbox;

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
    match sandbox::Launcher::find() {
        Some(sandbox::Launcher::Explicit(path)) => check(&sandbox::Launcher::Explicit(path)),
        Some(sandbox::Launcher::Pinned(checkout)) => {
            check(&sandbox::Launcher::Pinned(checkout.clone()));
            check(&sandbox::Launcher::Rust(Some(checkout.clone())));
        }
        Some(sandbox::Launcher::Rust(_)) => unreachable!(),
        None => {
            eprintln!("fleet-launch Python comparison skipped: no checkout of the pinned launcher under ~/code");
            check(&sandbox::Launcher::Rust(None));
        }
    }
    println!("fleet-launch parity passed: previews, refusals, launch records, session IDs and remote placement");
}

#[cfg(target_os = "linux")]
fn check(launcher: &sandbox::Launcher) {
    dry_run::check(launcher);
    refusals::check(launcher);
    records::check(launcher);
    records::check_id_resolution(launcher);
    remote::check(launcher);
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("fleet-launch parity skipped: requires Linux");
}
