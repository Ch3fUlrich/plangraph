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
    let Some(launcher) = sandbox::Launcher::find() else {
        eprintln!(
            "fleet-launch parity skipped: no launcher at ~/code/plangraph-data/router/bin/fleet-launch and PLANGRAPH_FLEET_LAUNCH is unset"
        );
        return;
    };
    dry_run::check(&launcher);
    refusals::check(&launcher);
    records::check(&launcher);
    records::check_id_resolution(&launcher);
    remote::check(&launcher);
    println!("fleet-launch parity passed: previews, refusals, launch records, session IDs and remote placement");
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("fleet-launch parity skipped: requires Linux");
}
