mod adapter;
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
