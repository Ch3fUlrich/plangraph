mod adapter;
mod agent_view;
mod agent_view_projection;
mod cli;
#[cfg(test)]
mod error_tests;
mod errors;
mod evidence;
mod evidence_row;
mod health_cli;
mod health_report;
mod health_report_rows;
mod jobs;
mod launch;
mod leg_ref;
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
    std::process::exit(cli::run(std::env::args_os().skip(1), probe));
}
