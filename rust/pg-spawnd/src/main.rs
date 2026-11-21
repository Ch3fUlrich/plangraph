mod adapter;
mod agent_view;
mod agent_view_projection;
mod cli;
mod cli_review;
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
mod jobs;
mod land_go;
mod land_go_shell;
#[cfg(test)]
mod land_go_tests;
mod launch;
mod leg_ref;
mod manage_key_alert;
mod native_steer;
mod native_writer;
mod native_writer_host;
mod native_writer_tool;
mod native_writer_tools;
mod placement;
mod pre_review;
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
mod review_citation;
mod review_retry;
mod review_split;
mod role_header;
mod round_progress;
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
mod work_mode_config;
mod writer_ledger;

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
