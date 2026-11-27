//! Read-only capability helper: no input, no mutation, no child commands.
//! Install root-owned with CAP_SYS_PTRACE only when /proc hides user-service cwds.
fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    #[cfg(target_os = "linux")]
    if args == ["--hygiene-references"] {
        return print_rows(pg_worker_core::disk_runtime::hygiene_references_direct());
    }
    let rows = match args.as_slice() {
        [] => pg_worker_core::disk_runtime::processes_direct(),
        [arg] if arg == "--references" => pg_worker_core::disk_runtime::process_references_direct(),
        _ => {
            eprintln!("usage: pg-process-cwds [--references|--hygiene-references]");
            return std::process::ExitCode::FAILURE;
        }
    };
    print_rows(rows)
}

fn print_rows(rows: std::io::Result<impl serde::Serialize>) -> std::process::ExitCode {
    match rows {
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
