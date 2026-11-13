use pg_worker_core::{
    hosts::HostRegistry,
    package_placement::{self, Policy},
};
use std::{
    env, io,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};
fn run() -> io::Result<bool> {
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::other("HOME required"))?;
    let mut registry = home.join(".config/plangraph/hosts.json");
    let mut state = home.join(".local/state/plangraph");
    let mut policy = Policy::default();
    let mut args = env::args().skip(1);
    while let Some(flag) = args.next() {
        if flag == "--allow-workstation" {
            policy.allow_workstation = true;
            continue;
        }
        let value = args
            .next()
            .ok_or_else(|| io::Error::other("missing option value"))?;
        match flag.as_str() {
            "--registry" => registry = value.into(),
            "--state" => state = value.into(),
            "--reservation-mb" => {
                policy.reservation_mb = value.parse().map_err(io::Error::other)?
            }
            _ => return Err(io::Error::other("unknown option")),
        }
    }
    let registry = HostRegistry::load(&registry).map_err(io::Error::other)?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?
        .as_secs();
    let choice = package_placement::choose(
        &package_placement::load(&registry, &state, now),
        &policy,
        now,
    );
    println!("{}", serde_json::to_string_pretty(&choice)?);
    Ok(choice.host.is_some())
}
fn main() -> std::process::ExitCode {
    match run() {
        Ok(true) => 0,
        Ok(false) => 5,
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
    .into()
}
