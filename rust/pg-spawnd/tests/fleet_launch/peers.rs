use super::sandbox::SID;
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::io;
use std::path::PathBuf;
use std::process;

pub fn invoke(name: &str, args: Vec<String>) -> bool {
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
        "powershell.exe" => print!("{}", read("windows-kb")),
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
                let payload: Value = serde_json::from_reader(io::stdin().lock()).unwrap();
                write("ssh.json", json!({"argv": args, "payload": payload}));
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
