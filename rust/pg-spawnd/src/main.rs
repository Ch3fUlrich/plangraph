#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

mod adapter;
mod agent_view;
mod agent_view_projection;
mod attempt_outcome;
mod cli;
mod cli_review;
mod doctor_sources;
mod empty_diff;
#[cfg(test)]
mod error_tests;
mod errors;
mod evidence;
mod evidence_row;
mod fleet_snapshot;
mod gitconfig_guard;
#[cfg(target_os = "linux")]
mod hand_run;
mod health_cli;
mod health_report;
mod health_report_rows;
mod host_tools;
mod install_check;
mod jobs;
mod land_go;
mod land_go_shell;
#[cfg(test)]
mod land_go_tests;
mod late_review;
mod launch;
mod leg_ref;
mod lifecycle_cli;
mod manage_key_alert;
mod native_steer;
mod native_writer;
mod native_writer_host;
mod native_writer_sandbox;
mod native_writer_tool;
mod native_writer_tools;
mod placement;
mod pre_review;
mod prompt;
mod prompt_git;
use pg_spawnd::pyjson;
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
mod review_health;
mod review_retry;
mod review_split;
mod review_test_evidence;
mod role_header;
mod round_progress;
mod round_replan;
mod round_replan_ports;
mod runs;
mod serve;
mod serve_options;
mod shim;
mod spawn;
use pg_spawnd::state;
#[cfg(test)]
mod state_tests;
mod tools;
#[cfg(unix)]
mod transport_unix;
#[cfg(windows)]
mod transport_windows;
mod work_mode_config;
mod worker_liveness;
mod writer_control;
mod writer_ledger;

use pg_worker_core::host_probe::probe;

fn main() {
    #[cfg(target_os = "linux")]
    if std::env::args_os().nth(1).as_deref() == Some(std::ffi::OsStr::new("session")) {
        let ops = std::env::var_os("PLANGRAPH_FLEET_OPS")
            .map(std::path::PathBuf::from)
            .or_else(|| {
                std::env::var_os("ROUTER_HOME")
                    .map(|router| std::path::PathBuf::from(router).join("ops"))
            })
            .unwrap_or_else(|| {
                std::env::current_exe()
                    .ok()
                    .and_then(|exe| Some(exe.parent()?.parent()?.join("ops")))
                    .filter(|ops| ops.is_dir())
                    .unwrap_or_else(|| "ops".into())
            });
        std::process::exit(pg_spawnd::session::run(
            std::env::args().skip(2).collect(),
            &ops,
        ));
    }
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
