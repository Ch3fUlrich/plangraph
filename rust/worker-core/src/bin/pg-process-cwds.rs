//! Read-only capability helper: no input, no mutation, no child commands.
//! Install root-owned with CAP_SYS_PTRACE only when /proc hides user-service cwds.
fn main() -> std::process::ExitCode {
    if std::env::args_os().count() != 1 {
        eprintln!("pg-process-cwds accepts no arguments");
        return std::process::ExitCode::FAILURE;
    }
    match pg_worker_core::disk_runtime::processes_direct() {
        Ok(rows) => match serde_json::to_string(&rows) {
            Ok(json) => {
                println!("{json}");
                std::process::ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("process inventory: {e}");
                std::process::ExitCode::FAILURE
            }
        },
        Err(e) => {
            eprintln!("process inventory: {e}");
            std::process::ExitCode::FAILURE
        }
    }
}
