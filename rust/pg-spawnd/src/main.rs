mod adapter;
mod cli;
#[cfg(test)]
mod error_tests;
mod errors;
mod placement;
mod pyjson;
mod records;
mod serve;
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
