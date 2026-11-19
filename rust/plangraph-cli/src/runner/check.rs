//! Section C.6 self-checks: probe facts per runner user, evaluate purely.
use super::config::Config;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Facts {
    pub user: String,
    pub home_matches: bool,
    pub python_imports: bool,
    pub chrome_present: bool,
    /// None: binary not fetched yet (setup-temporal downloads it in the first run).
    pub temporal_version_ok: Option<bool>,
    pub run_one: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Status {
    Pass,
    Warn,
    Fail,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Check {
    pub user: String,
    pub name: &'static str,
    pub status: Status,
}

pub fn evaluate(f: &Facts) -> Vec<Check> {
    let mk = |name, ok| Check {
        user: f.user.clone(),
        name,
        status: if ok { Status::Pass } else { Status::Fail },
    };
    let cache_env = f.run_one.as_deref().is_some_and(|s| {
        s.contains("export RUNNER_TOOL_CACHE=$HOME/hostedtoolcache")
            && s.contains("export AGENT_TOOLSDIRECTORY=$HOME/hostedtoolcache")
    });
    let temporal = match f.temporal_version_ok {
        Some(ok) => mk("temporal --version", ok),
        None => Check {
            user: f.user.clone(),
            name: "temporal --version",
            status: Status::Warn,
        },
    };
    vec![
        mk("passwd home matches configured home_base", f.home_matches),
        mk(
            "scrubbed-env python imports ctypes and yaml",
            f.python_imports,
        ),
        mk("playwright chrome file exists", f.chrome_present),
        temporal,
        mk("listener env has RUNNER_TOOL_CACHE", cache_env),
    ]
}

/// Host-level probe: other users must not see the runners' process arguments
/// (the JIT token is passed on the run.sh command line).
pub fn proc_hidepid_check(mounts: &str) -> Check {
    Check {
        user: "host".into(),
        name: "/proc mounted with hidepid=2",
        status: if super::observe::hidepid_active(mounts) {
            Status::Pass
        } else {
            Status::Fail
        },
    }
}

pub fn failed(checks: &[Check]) -> bool {
    checks.iter().any(|c| c.status == Status::Fail)
}

pub fn render(checks: &[Check]) -> String {
    let mut out = String::new();
    for c in checks {
        let tag = match c.status {
            Status::Pass => "ok  ",
            Status::Warn => "warn",
            Status::Fail => "FAIL",
        };
        out.push_str(&format!("{tag} {} {}\n", c.user, c.name));
    }
    let n = checks.iter().filter(|c| c.status == Status::Fail).count();
    out.push_str(&format!("{} checks, {n} failed\n", checks.len()));
    out
}

fn find_chrome(dir: &Path, depth: u32) -> bool {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return false;
    };
    rd.flatten().any(|e| {
        let name = e.file_name().to_string_lossy().into_owned();
        let p = e.path();
        if p.is_dir() {
            depth > 0 && find_chrome(&p, depth - 1)
        } else {
            name == "chrome" || name == "chrome-headless-shell" || name == "headless_shell"
        }
    })
}

fn as_user_ok(user: &str, argv: &[String]) -> bool {
    Command::new("runuser")
        .args(["-u", user, "--"])
        .args(argv)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|s| s.success())
}

pub fn probe(cfg: &Config, user: &str) -> Facts {
    let home = cfg.home(user);
    let home_matches = super::observe::passwd_home(user).as_deref() == Some(home.as_path());
    let python = cfg.seed_python().to_string_lossy().into_owned();
    let scrubbed = |rest: Vec<String>| {
        let mut v = vec![
            "env".to_string(),
            "-i".into(),
            format!("HOME={}", home.display()),
        ];
        v.push("PATH=/usr/bin:/bin".into());
        v.extend(rest);
        v
    };
    let temporal: PathBuf = home.join(".cache/temporal-cli/temporal");
    Facts {
        user: user.into(),
        home_matches,
        python_imports: as_user_ok(
            user,
            &scrubbed(vec![python, "-c".into(), "import ctypes, yaml".into()]),
        ),
        chrome_present: find_chrome(&home.join(".cache/ms-playwright"), 3),
        temporal_version_ok: temporal.exists().then(|| {
            as_user_ok(
                user,
                &scrubbed(vec![
                    temporal.to_string_lossy().into_owned(),
                    "--version".into(),
                ]),
            )
        }),
        run_one: std::fs::read_to_string(home.join("run-one.sh")).ok(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runner::template;

    fn good() -> Facts {
        Facts {
            user: "runner1".into(),
            home_matches: true,
            python_imports: true,
            chrome_present: true,
            temporal_version_ok: Some(true),
            run_one: Some(template::run_one("127.0.0.1:1")),
        }
    }
    #[test]
    fn all_good_passes() {
        let c = evaluate(&good());
        assert!(!failed(&c));
        assert!(render(&c).ends_with("5 checks, 0 failed\n"));
    }
    #[test]
    fn each_broken_fact_fails_that_check() {
        let mut f = good();
        f.python_imports = false;
        f.chrome_present = false;
        f.run_one = Some("export PATH=x".into());
        let c = evaluate(&f);
        assert!(failed(&c));
        let r = render(&c);
        assert_eq!(r.matches("FAIL").count(), 3);
        assert!(r.contains("FAIL runner1 playwright chrome file exists"));
        assert!(r.ends_with("5 checks, 3 failed\n"));
    }
    #[test]
    fn mismatched_passwd_home_fails_check() {
        let mut f = good();
        f.home_matches = false;
        let c = evaluate(&f);
        assert!(failed(&c));
        assert!(render(&c).contains("FAIL runner1 passwd home matches configured home_base"));
    }
    #[test]
    fn missing_temporal_binary_warns_but_a_failing_one_fails() {
        let mut f = good();
        f.temporal_version_ok = None;
        let c = evaluate(&f);
        assert!(!failed(&c) && render(&c).contains("warn runner1 temporal"));
        f.temporal_version_ok = Some(false);
        assert!(failed(&evaluate(&f)));
    }
    #[test]
    fn missing_run_one_fails() {
        let mut f = good();
        f.run_one = None;
        assert!(failed(&evaluate(&f)));
    }
    #[test]
    fn hidepid_probe_fails_without_the_mount_option() {
        assert!(failed(&[proc_hidepid_check("proc /proc proc rw 0 0\n")]));
        assert!(!failed(&[proc_hidepid_check(
            "proc /proc proc rw,hidepid=2 0 0\n"
        )]));
    }
}
