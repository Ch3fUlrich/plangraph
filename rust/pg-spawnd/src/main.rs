mod cli;
mod serve;
mod shim;
mod tools;
#[cfg(unix)]
mod transport_unix;
#[cfg(windows)]
mod transport_windows;

use pg_worker_core::host_probe::probe;

fn main() {
    std::process::exit(cli::run(std::env::args_os().skip(1), probe));
}
