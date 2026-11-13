mod adapter;
mod agent_view;
mod agent_view_projection;
mod cli;
mod doctor_sources;
#[cfg(test)]
mod error_tests;
mod errors;
mod evidence;
mod evidence_row;
mod fleet_snapshot;
mod gitconfig_guard;
mod health_cli;
mod health_report;
mod health_report_rows;
mod host_readiness;
mod jobs;
mod launch;
mod leg_ref;
mod package;
mod package_probe;
mod package_probe_scripts;
mod package_scripts;
mod package_transport;
mod package_windows;
mod placement;
mod prompt;
mod prompt_git;
mod pyjson;
mod reconcile;
#[allow(dead_code)]
mod reconcile_attach;
#[cfg(unix)]
#[allow(dead_code)] // Callable bridges; locked startup ownership follows separately.
mod reconcile_backend;
mod reconcile_serve;
mod reconcile_store;
mod reconcile_time;
#[cfg(unix)]
#[allow(dead_code)]
mod reconcile_watch;
mod records;
mod review_artifacts;
mod runs;
mod serve;
mod serve_options;
mod shim;
mod spawn;
mod state;
#[cfg(test)]
mod state_tests;
mod tools;
#[cfg(unix)]
mod transport_unix;
#[cfg(windows)]
mod transport_windows;

use pg_worker_core::host_probe::probe;

fn main() {
    #[cfg(windows)]
    if let Ok(path) = std::env::current_exe() {
        pg_worker_core::run::job_holder_windows::set_holder_path(path);
    }
    #[cfg(windows)]
    if std::env::args_os().nth(1).as_deref() == Some(std::ffi::OsStr::new("__job-holder")) {
        std::process::exit(pg_worker_core::run::job_holder_windows::helper());
    }
    std::process::exit(cli::run(std::env::args_os().skip(1), probe));
}
