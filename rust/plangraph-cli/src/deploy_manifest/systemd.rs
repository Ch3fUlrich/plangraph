//! The one door to systemd. Every read of live unit state and every enable goes through [`Systemctl`], so
//! tests pass a fixture and never touch systemd or ssh.
use super::Scope;
use std::process::Command;

/// One `systemctl` call: its exit code and output.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Out {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

pub trait Systemctl {
    /// Run `systemctl [--user] ARGS`. `Err` means systemctl could not run at all (not installed, no bus).
    fn run(&self, scope: Scope, args: &[&str]) -> Result<Out, String>;
}

/// systemctl on this host.
pub struct HostSystemctl;

impl Systemctl for HostSystemctl {
    fn run(&self, scope: Scope, args: &[&str]) -> Result<Out, String> {
        let mut command = Command::new("systemctl");
        if scope == Scope::User {
            command.arg("--user");
        }
        let output = command
            .args(args)
            .output()
            .map_err(|e| format!("systemctl: {e}"))?;
        Ok(Out {
            code: output.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        })
    }
}

/// A scripted systemctl for tests: answers by the argument list, records every call.
#[cfg(test)]
pub mod fixture {
    use super::*;
    use std::cell::RefCell;
    use std::collections::BTreeMap;

    #[derive(Default)]
    pub struct Fixture {
        pub answers: BTreeMap<String, Out>,
        pub calls: RefCell<Vec<String>>,
        pub unavailable: bool,
    }

    impl Fixture {
        pub fn answer(&mut self, call: &str, code: i32, stdout: &str) {
            self.answers.insert(
                call.into(),
                Out {
                    code,
                    stdout: stdout.into(),
                    stderr: String::new(),
                },
            );
        }
    }

    impl Systemctl for Fixture {
        fn run(&self, scope: Scope, args: &[&str]) -> Result<Out, String> {
            if self.unavailable {
                return Err("systemctl: not found".into());
            }
            let key = format!(
                "{}{}",
                if scope == Scope::User { "--user " } else { "" },
                args.join(" ")
            );
            self.calls.borrow_mut().push(key.clone());
            Ok(self.answers.get(&key).cloned().unwrap_or(Out {
                code: 0,
                ..Out::default()
            }))
        }
    }
}
