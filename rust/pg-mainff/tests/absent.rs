#![cfg(unix)]
mod common;
use common::{git, write, Fixture};
use pg_mainff::{
    advance,
    process::{Error, Output, Runner, System},
};
use std::{cell::Cell, fs, path::Path};
struct Host<'a> {
    case: &'a str,
    process: i32,
    scopes: Cell<u32>,
}
fn output(code: i32, out: &str, err: &str) -> Result<Output, Error> {
    Ok(Output {
        code,
        out: out.into(),
        err: err.into(),
    })
}
impl Runner for Host<'_> {
    fn run(&self, args: &[&str]) -> Result<Output, Error> {
        match args[0] {
            "systemctl" => {
                let user = args.contains(&"--user");
                self.scopes
                    .set(self.scopes.get() | if user { 1 } else { 2 });
                match self.case {
                    "missing-systemctl" => Err(Error::Missing),
                    "timeout" => Err(Error::Timeout),
                    "oserror" => Err(Error::Unavailable),
                    "bus" => output(1, "", "Failed to connect to bus: unavailable"),
                    "system-pid" if !user => output(0, "LoadState=loaded\nMainPID=123\n", ""),
                    "loaded-zero" => output(0, "LoadState=loaded\nMainPID=0\n", ""),
                    "bad-unit" => output(0, "LoadState=loaded\nMainPID=invalid\n", ""),
                    "exit" => output(1, "", "private fixture cause"),
                    _ => output(0, "LoadState=not-found\nMainPID=0\n", ""),
                }
            }
            "ps" | "powershell.exe" => match self.process {
                -1 => Err(Error::Missing),
                -2 => Err(Error::Unavailable),
                -3 => Err(Error::Timeout),
                -4 => output(1, "", "private process failure"),
                -5 => output(0, "bad fixture data", ""),
                pid if pid > 0 => output(
                    0,
                    &format!("{pid} python3 python3 src/plangraph/keeper/daemon.py\n"),
                    "",
                ),
                _ => output(0, "", ""),
            },
            _ => System.run(args),
        }
    }
}
impl Host<'_> {
    fn check(&self, f: &Fixture) -> (i32, String) {
        advance::main_ff(self, &f.repo, &f.state, false, "plangraph-keeper.service")
    }
}
#[test]
fn absent_state_checks_unit_scopes_and_process() {
    for (case, process, held, cause) in [
        ("missing-systemctl", 0, false, ""),
        ("not-found", 0, false, ""),
        ("bus", 0, true, "bus unreachable"),
        ("timeout", 0, true, "timed out"),
        ("oserror", 0, true, "unit query unavailable"),
        ("bad-unit", 0, true, "state unreadable"),
        ("exit", 0, true, "query failed"),
        ("bus", 123, true, "live keeper"),
        ("not-found", 123, true, "live keeper"),
        ("loaded-zero", 123, true, "live keeper"),
        ("missing-systemctl", 123, true, "live keeper"),
        ("system-pid", 0, true, "live keeper"),
    ] {
        let f = Fixture::new();
        let target = f.target("src/plangraph/spawner/unused.py", "VALUE = 2\n");
        fs::remove_file(&f.state).unwrap();
        let host = Host {
            case,
            process,
            scopes: Cell::new(0),
        };
        let (code, line) = host.check(&f);
        assert_eq!(code, if held { 3 } else { 0 }, "{case} {line}");
        assert_eq!(f.head(), if held { f.loaded.clone() } else { target });
        assert!(line.contains(cause), "{line}");
        if matches!(case, "not-found" | "loaded-zero" | "system-pid") {
            assert_eq!(host.scopes.get(), 3);
        }
    }
}
#[test]
fn missing_and_wrong_db_state_hold_for_live_keeper() {
    for wrong_db in [false, true] {
        let mut f = Fixture::new();
        f.target("src/plangraph/keeper/daemon.py", "VALUE = 2\n");
        if wrong_db {
            f.state = f.state.with_file_name("wrong-db.loaded.json");
        } else {
            fs::remove_file(&f.state).unwrap();
        }
        let host = Host {
            case: "not-found",
            process: 123,
            scopes: Cell::new(0),
        };
        let (code, line) = host.check(&f);
        assert_eq!(code, 3);
        assert!(line.contains("restart keeper") && line.contains("state"));
        assert_eq!(f.head(), f.loaded);
    }
}
#[test]
fn process_probe_failures_hold_with_fixed_cause() {
    for (process, cause) in [
        (-1, "unavailable"),
        (-2, "unavailable"),
        (-3, "timed out"),
        (-4, "failed"),
        (-5, "unavailable"),
    ] {
        let f = Fixture::new();
        f.target("src/plangraph/spawner/unused.py", "VALUE = 2\n");
        fs::remove_file(&f.state).unwrap();
        let host = Host {
            case: "not-found",
            process,
            scopes: Cell::new(0),
        };
        let (code, line) = host.check(&f);
        assert_eq!(code, 3);
        assert!(
            line.contains(&format!("keeper process query {cause}")),
            "{line}"
        );
        assert!(!line.contains("private"));
        assert_eq!(f.head(), f.loaded);
    }
}
#[test]
fn reused_pid_state_is_not_treated_as_running_keeper() {
    let f = Fixture::new();
    let target = f.target("src/plangraph/keeper/daemon.py", "VALUE = 2\n");
    fs::write(&f.state,serde_json::json!({"pid":std::process::id(),"loaded_sha":f.loaded,"pid_start":"different-process-start"}).to_string()).unwrap();
    let host = Host {
        case: "not-found",
        process: 0,
        scopes: Cell::new(0),
    };
    assert_eq!(host.check(&f).0, 0);
    assert_eq!(f.head(), target);
}
struct Restart<'a> {
    host: Host<'a>,
    state: &'a Path,
    loaded: &'a str,
    calls: Cell<u32>,
}
impl Runner for Restart<'_> {
    fn run(&self, args: &[&str]) -> Result<Output, Error> {
        if args[0] == "ps" {
            self.calls.set(self.calls.get() + 1);
            fs::write(
                self.state,
                serde_json::json!({"pid":std::process::id(),"loaded_sha":self.loaded}).to_string(),
            )
            .unwrap();
        }
        self.host.run(args)
    }
}
#[test]
fn restart_state_is_rechecked_before_merge() {
    let f = Fixture::new();
    f.target("src/plangraph/keeper/daemon.py", "VALUE = 2\n");
    fs::remove_file(&f.state).unwrap();
    let run = Restart {
        host: Host {
            case: "not-found",
            process: 0,
            scopes: Cell::new(0),
        },
        state: &f.state,
        loaded: &f.loaded,
        calls: Cell::new(0),
    };
    let result = advance::main_ff(&run, &f.repo, &f.state, false, "plangraph-keeper.service");
    assert_eq!(result.0, 3, "{}", result.1);
    assert_eq!(run.calls.get(), 1);
    assert_eq!(f.head(), f.loaded);
}
#[test]
fn existing_keeper_runtime_data_holds() {
    for path in [
        "src/plangraph/routing_support/catalog_schema.sql",
        "src/plangraph/planner/route_policy.json",
        "configuration/opencode/providers.json",
    ] {
        let mut f = Fixture::new();
        write(&f.repo, path, "old data\n");
        git(&f.repo, &["add", "."]);
        git(&f.repo, &["commit", "-m", "loaded data"]);
        f.loaded = git(&f.repo, &["rev-parse", "HEAD"]);
        fs::write(
            &f.state,
            serde_json::json!({"pid":std::process::id(),"loaded_sha":f.loaded}).to_string(),
        )
        .unwrap();
        f.target(path, "new data\n");
        let (code, line) = f.run();
        assert_eq!(code, 3);
        assert!(line.contains(path));
        assert_eq!(f.head(), f.loaded);
    }
}
