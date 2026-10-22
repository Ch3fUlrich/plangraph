use std::ffi::OsString;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::Command;

use pg_jail::{JailError, JailSpec};

fn main() {
    if let Err(code) = run() {
        std::process::exit(code);
    }
}

fn run() -> Result<(), i32> {
    let mut spec = JailSpec::default();
    let mut args = std::env::args_os().skip(1);
    let mut command = None;

    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--") => {
                command = Some(args.collect::<Vec<_>>());
                break;
            }
            Some("--rw") => spec.rw.push(next_path(&mut args)?),
            Some("--ro") => spec.ro.push(next_path(&mut args)?),
            Some("--deny-read") => spec.deny_read.push(next_path(&mut args)?),
            Some("--best-effort") => spec.best_effort = true,
            _ => return Err(usage("unexpected argument")),
        }
    }

    let mut command = command.ok_or_else(|| usage("missing -- CMD"))?;
    if command.is_empty() {
        return Err(usage("missing command"));
    }
    let program = command.remove(0);

    if let Err(e) = pg_jail::apply(&spec) {
        eprintln!("pg-jail: {e}");
        return Err(match e {
            JailError::Unsupported => 78,
            _ => 126,
        });
    }

    let err = Command::new(&program).args(command).exec();
    eprintln!("pg-jail: {err}");
    Err(127)
}

fn next_path<I>(args: &mut I) -> Result<PathBuf, i32>
where
    I: Iterator<Item = OsString>,
{
    args.next()
        .map(PathBuf::from)
        .ok_or_else(|| usage("missing path after option"))
}

fn usage(msg: &str) -> i32 {
    eprintln!("pg-jail: {msg}");
    64
}
