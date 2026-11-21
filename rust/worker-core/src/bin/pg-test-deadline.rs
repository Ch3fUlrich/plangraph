//! Cargo runner for libtest binaries. Give each case its own deadline and process group.
#[path = "test_deadline/children.rs"]
mod children;

use pg_worker_core::stall::{supervise, supervise_with_diagnostics, Opts};
use std::{
    env,
    ffi::{OsStr, OsString},
    io::{self, Write},
    path::Path,
    process::{Command, ExitCode},
    time::Duration,
};

const DEFAULT_SECONDS: u64 = 120;

fn stack(pid: u32) {
    #[cfg(target_os = "linux")]
    {
        let mut debugger = Command::new("gdb");
        debugger.args([
            "--batch",
            "--nx",
            "-iex",
            "set auto-load off",
            "-ex",
            "thread apply all bt",
            "-p",
            &pid.to_string(),
        ]);
        let opts = Opts {
            timeout: Some(Duration::from_secs(8)),
            grace: Duration::from_millis(250),
            ..Opts::default()
        };
        match supervise(debugger, &opts) {
            Ok(dump) => {
                let has_frames = dump
                    .output
                    .split(|&byte| byte == b'\n')
                    .any(|line| line.starts_with(b"#0 "));
                if has_frames {
                    eprintln!("stack for test process {pid}:");
                }
                let _ = io::stderr().write_all(&dump.output);
                if has_frames {
                    return;
                }
            }
            Err(error) => eprintln!("gdb unavailable: {error}"),
        }
        eprintln!("user-space stack unavailable for test process {pid}");
        if let Ok(threads) = std::fs::read_dir(format!("/proc/{pid}/task")) {
            for thread in threads.flatten() {
                let tid = thread.file_name();
                if let Ok(stack) = std::fs::read_to_string(thread.path().join("stack")) {
                    eprintln!(
                        "thread {} kernel stack: {}",
                        tid.to_string_lossy(),
                        stack.trim()
                    );
                } else if let Ok(channel) = std::fs::read_to_string(thread.path().join("wchan")) {
                    eprintln!(
                        "thread {} kernel wait channel: {}",
                        tid.to_string_lossy(),
                        channel.trim()
                    );
                }
            }
        }
    }
    #[cfg(not(target_os = "linux"))]
    eprintln!("user-space stack unavailable for test process {pid}");
}

fn debug_child(cmd: &mut Command) {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        // Yama permits the named ptracer and its descendants. GDB is launched
        // by the test's parent, so it inherits that permission.
        unsafe {
            cmd.pre_exec(|| {
                libc::prctl(libc::PR_SET_PTRACER, libc::getppid(), 0, 0, 0);
                Ok(())
            });
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = cmd;
}

fn cases(binary: &OsStr, args: &[OsString], opts: &Opts) -> io::Result<Vec<String>> {
    let mut list = Command::new(binary);
    list.args(args).arg("--list");
    let listing = supervise(list, opts)?;
    if listing.code != Some(0) {
        return Err(io::Error::other(format!(
            "cannot list {}: {}",
            binary.to_string_lossy(),
            String::from_utf8_lossy(&listing.output)
        )));
    }
    Ok(String::from_utf8_lossy(&listing.output)
        .lines()
        .filter_map(|line| {
            line.strip_suffix(": test")
                .or_else(|| line.strip_suffix(": benchmark"))
        })
        .map(str::to_owned)
        .collect())
}

fn run() -> io::Result<bool> {
    let mut args = env::args_os().skip(1);
    let binary = args
        .next()
        .ok_or_else(|| io::Error::other("expected a libtest binary"))?;
    let args: Vec<_> = args.collect();
    if args
        .iter()
        .any(|arg| matches!(arg.to_str(), Some("--list" | "-h" | "--help")))
    {
        return Ok(Command::new(binary).args(args).status()?.success());
    }
    let seconds = match env::var("PLANGRAPH_TEST_DEADLINE") {
        Ok(value) => value
            .parse::<u64>()
            .ok()
            .filter(|&seconds| (1..=DEFAULT_SECONDS).contains(&seconds))
            .ok_or_else(|| io::Error::other("PLANGRAPH_TEST_DEADLINE must shorten the default"))?,
        Err(env::VarError::NotPresent) => DEFAULT_SECONDS,
        Err(err) => return Err(io::Error::other(err)),
    };
    let opts = Opts {
        timeout: Some(Duration::from_secs(seconds)),
        grace: Duration::from_secs(1),
        max_output: 8 << 20,
        ..Opts::default()
    };
    let listed = cases(&binary, &args, &opts)?;
    let stem = Path::new(&binary)
        .file_stem()
        .and_then(OsStr::to_str)
        .unwrap_or("libtest");
    let suite = stem.rsplit_once('-').map_or(stem, |(name, _)| name);
    children::adopt()?;
    let mut failures = 0;
    for (index, case) in listed.iter().enumerate() {
        let mut cmd = Command::new(&binary);
        // The original filters were already evaluated by --list. Forward options only.
        let mut flags = args.iter();
        while let Some(arg) = flags.next() {
            if let Some(flag) = arg.to_str().filter(|s| s.starts_with('-')) {
                cmd.arg(arg);
                if matches!(
                    flag,
                    "--skip" | "--test-threads" | "--color" | "--format" | "-Z" | "--shuffle-seed"
                ) {
                    cmd.args(flags.next());
                }
            }
        }
        if !args.iter().any(|arg| arg == "--exact") {
            cmd.arg("--exact");
        }
        cmd.arg(case);
        debug_child(&mut cmd);
        let result = supervise_with_diagnostics(
            cmd,
            &opts,
            &|| false,
            |pid| {
                eprintln!("HUNG {suite}::{case}");
                stack(pid);
            },
            children::reap,
        )?;
        if result.timed_out || result.code != Some(0) {
            failures += 1;
            io::stderr().write_all(&result.output)?;
            if result.timed_out {
                println!("{suite}: {} cases, {failures} failed", index + 1);
                return Ok(false);
            }
        } else if args.iter().any(|arg| {
            matches!(
                arg.to_str(),
                Some("--nocapture" | "--no-capture" | "--show-output")
            )
        }) {
            io::stdout().write_all(&result.output)?;
        }
    }
    println!("{suite}: {} cases, {failures} failed", listed.len());
    Ok(failures == 0)
}

fn main() -> ExitCode {
    match run() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("pg-test-deadline: {error}");
            ExitCode::FAILURE
        }
    }
}
