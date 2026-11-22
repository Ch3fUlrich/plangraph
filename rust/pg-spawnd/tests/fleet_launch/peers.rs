use super::sandbox::SID;
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::io::{self, Read};
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process;
pub fn invoke(name: &str, args: Vec<String>) -> bool {
    if name == "fleet-launch" {
        let error = process::Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .arg("session")
            .args(args)
            .exec();
        panic!("starting Rust destination: {error}");
    }
    if !matches!(name, "claude" | "ssh" | "powershell.exe") {
        return false;
    }
    let root = PathBuf::from(env::var_os("FIXTURE_ROOT").unwrap());
    let read = |file| fs::read_to_string(root.join(file)).unwrap();
    let write = |file, value: Value| fs::write(root.join(file), value.to_string()).unwrap();
    let env_value = |key| env::var(key).ok();
    match name {
        "claude" if args == ["agents", "--json"] => {
            print!(
                "{}",
                fs::read_to_string(root.join("agents.json")).unwrap_or_else(|_| "[]".into())
            );
        }
        "claude" => {
            assert_eq!(args.first().map(String::as_str), Some("--bg"));
            write(
                "child.json",
                json!({
                    "argv": args, "cwd": env::current_dir().unwrap(),
                    "role": env_value("FLEET_SESSION_NAME"),
                    "window": env_value("CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
                    "forwarded": env_value("FLEET_FORWARDED"),
                    "defer": env_value("FLEET_DEFER_RECORD"),
                    "path": env_value("PATH"),
                }),
            );
            print!(
                "{}",
                fs::read_to_string(root.join("child.stdout"))
                    .unwrap_or_else(|_| format!("{SID}\n"))
            );
            if root.join("child.rc").exists() {
                eprintln!("fixture child failure");
                process::exit(read("child.rc").trim().parse().unwrap());
            }
        }
        "powershell.exe" => {
            fs::write(root.join("interop"), "").unwrap();
            print!("{}", read("windows-kb"));
        }
        "ssh" => {
            let command = args.last().unwrap();
            if command.contains("Get-CimInstance") {
                print!("{}", read("windows-kb"));
            } else if command.contains("/proc/meminfo") {
                let host = &args[args.len() - 2];
                print!("{}", read(&format!("memory-{host}")));
            } else {
                assert!(
                    command.starts_with("bash -lc "),
                    "unexpected ssh command: {args:?}"
                );
                assert!(!command.contains('\n'), "ssh command exceeds one line");
                let mut wire = String::new();
                io::stdin().lock().read_to_string(&mut wire).unwrap();
                let payload: Value = serde_json::from_str(&wire).unwrap();
                write(
                    "ssh.json",
                    json!({"argv": args, "payload": payload, "wire": wire}),
                );
                print!("{}", read("ssh.stdout"));
                if root.join("ssh.stderr").exists() {
                    eprint!("{}", read("ssh.stderr"));
                }
            }
        }
        _ => unreachable!(),
    }
    true
}
