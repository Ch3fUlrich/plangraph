//! Action -> concrete steps (pure), and the executor. Commands use explicit
//! argument vectors; no shell string is ever built from input.
use super::config::Config;
use super::plan::{cache_file, Action, CACHE_DIR, DL_RUNNER, STATE_DIR};
use std::path::{Path, PathBuf};

pub const SEED_MARKER: &str = "seed-marker";
pub const DEPS_MARKER: &str = "playwright-deps";

#[derive(Clone, Debug, PartialEq)]
pub enum Step {
    Cmd {
        program: String,
        args: Vec<String>,
        env: Vec<(String, String)>,
        cwd: Option<PathBuf>,
    },
    WriteFile {
        path: PathBuf,
        content: String,
        mode: u32,
    },
    Mkdir(PathBuf),
    Symlink {
        target: PathBuf,
        link: PathBuf,
    },
    Download {
        url: String,
        dest: PathBuf,
        sha256: String,
        size: Option<u64>,
    },
    Remove(PathBuf),
    /// Append `line` to the file unless an identical line is already there.
    EnsureLine {
        path: PathBuf,
        line: String,
    },
}

fn s(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}
fn cmd(program: &str, args: &[&str]) -> Step {
    Step::Cmd {
        program: program.into(),
        args: args.iter().map(|a| a.to_string()).collect(),
        env: vec![],
        cwd: None,
    }
}
fn cmd_env(program: &str, args: Vec<String>, env: &[(&str, String)], cwd: Option<PathBuf>) -> Step {
    Step::Cmd {
        program: program.into(),
        args,
        env: env
            .iter()
            .map(|(k, v)| (k.to_string(), v.clone()))
            .collect(),
        cwd,
    }
}
fn chown(owner: &str, paths: &[&Path], recursive: bool) -> Step {
    let mut args: Vec<String> = Vec::new();
    args.push(if recursive { "-R" } else { "-h" }.into());
    args.push(owner.into());
    args.extend(paths.iter().map(|p| s(p)));
    Step::Cmd {
        program: "chown".into(),
        args,
        env: vec![],
        cwd: None,
    }
}
fn as_user(cfg: &Config, user: &str, env: &[(&str, String)], argv: Vec<String>) -> Step {
    let mut args = vec!["-u".to_string(), user.into(), "--".into(), "env".into()];
    args.push(format!("HOME={}", s(&cfg.home(user))));
    args.extend(env.iter().map(|(k, v)| format!("{k}={v}")));
    args.extend(argv);
    cmd_env("runuser", args, &[], None)
}

pub fn steps_for(cfg: &Config, action: &Action) -> Vec<Step> {
    let state = PathBuf::from(STATE_DIR);
    match action {
        Action::AptInstall(pkgs) => {
            let env = [("DEBIAN_FRONTEND", "noninteractive".to_string())];
            let lock = vec!["-o".to_string(), "DPkg::Lock::Timeout=300".into()];
            let mut update = lock.clone();
            update.push("update".into());
            let mut install = lock;
            install.extend(["install", "-y", "--no-install-recommends"].map(String::from));
            install.extend(pkgs.iter().cloned());
            vec![
                cmd_env("apt-get", update, &env, None),
                cmd_env("apt-get", install, &env, None),
            ]
        }
        Action::CreateUser(u) => vec![
            Step::Mkdir(cfg.home_base.clone()),
            cmd(
                "useradd",
                &["-m", "-d", &s(&cfg.home(u)), "-s", "/bin/bash", u],
            ),
        ],
        Action::EnableLinger(u) => vec![cmd("loginctl", &["enable-linger", u])],
        Action::InstallSudoers { content } => {
            let staged = state.join("ci-apt.new");
            vec![
                Step::Mkdir(state.clone()),
                Step::WriteFile {
                    path: staged.clone(),
                    content: content.clone(),
                    mode: 0o440,
                },
                cmd("visudo", &["-cf", &s(&staged)]),
                cmd(
                    "install",
                    &[
                        "-m",
                        "0440",
                        "-o",
                        "root",
                        "-g",
                        "root",
                        &s(&staged),
                        super::plan::SUDOERS_FILE,
                    ],
                ),
                Step::Remove(staged),
            ]
        }
        Action::HidepidProc => vec![
            Step::EnsureLine {
                path: PathBuf::from("/etc/fstab"),
                line: super::plan::FSTAB_LINE.into(),
            },
            cmd("mount", &["-o", "remount,hidepid=2", "/proc"]),
        ],
        Action::Download {
            name,
            url,
            sha256,
            size,
        } => vec![
            Step::Mkdir(PathBuf::from(CACHE_DIR)),
            Step::Download {
                url: url.clone(),
                dest: cache_file(name),
                sha256: sha256.clone(),
                size: *size,
            },
        ],
        Action::UnpackRunner { user } => {
            let dir = cfg.home(user).join("runner");
            let owner = format!("{user}:{user}");
            vec![
                Step::Remove(dir.clone()),
                Step::Mkdir(dir.clone()),
                cmd("tar", &["-xzf", &s(&cache_file(DL_RUNNER)), "-C", &s(&dir)]),
                chown(&owner, &[&dir], true),
                Step::WriteFile {
                    path: dir.join(".plangraph-version"),
                    content: cfg.actions_runner.version.clone(),
                    mode: 0o644,
                },
            ]
        }
        Action::SeedPython { marker } => seed_steps(cfg, marker),
        Action::HostedToolcache { user } => {
            let dir = cfg.home(user).join("hostedtoolcache");
            let owner = format!("{user}:{user}");
            vec![
                Step::Mkdir(dir.clone()),
                Step::Symlink {
                    target: cfg.seed_dir(),
                    link: dir.join("Python"),
                },
                chown(&owner, &[&dir], false),
                chown(&owner, &[&dir.join("Python")], false),
            ]
        }
        Action::PlaywrightChromium { user } => {
            let browsers = cfg.home(user).join(".cache/ms-playwright");
            vec![
                Step::Mkdir(cfg.home(user).join(".cache")),
                chown(
                    &format!("{user}:{user}"),
                    &[&cfg.home(user).join(".cache")],
                    false,
                ),
                as_user(
                    cfg,
                    user,
                    &[("PLAYWRIGHT_BROWSERS_PATH", s(&browsers))],
                    vec![
                        s(&cfg.seed_python()),
                        "-m".into(),
                        "playwright".into(),
                        "install".into(),
                        "chromium".into(),
                    ],
                ),
            ]
        }
        Action::PlaywrightDeps => vec![
            cmd(
                &s(&cfg.seed_python()),
                &["-m", "playwright", "install-deps", "chromium"],
            ),
            Step::Mkdir(state.clone()),
            Step::WriteFile {
                path: state.join(DEPS_MARKER),
                content: "done\n".into(),
                mode: 0o644,
            },
        ],
        Action::TemporalLink { user } => {
            let home = cfg.home(user);
            let owner = format!("{user}:{user}");
            let link = home.join(".cache/temporal-cli");
            vec![
                Step::Mkdir(home.join(".cache")),
                Step::Symlink {
                    target: home.join("runner/.cache/temporal-cli"),
                    link: link.clone(),
                },
                chown(&owner, &[&home.join(".cache")], false),
                chown(&owner, &[&link], false),
            ]
        }
        Action::RunOne { user, content } => {
            let path = cfg.home(user).join("run-one.sh");
            vec![
                Step::WriteFile {
                    path: path.clone(),
                    content: content.clone(),
                    mode: 0o755,
                },
                chown(&format!("{user}:{user}"), &[&path], false),
            ]
        }
    }
}

fn seed_steps(cfg: &Config, marker: &str) -> Vec<Step> {
    let work = PathBuf::from(CACHE_DIR).join("python-seed-extract");
    let cache = &cfg.python_seed.tool_cache;
    let mut v = vec![
        Step::Mkdir(PathBuf::from(STATE_DIR)),
        Step::Remove(work.clone()),
        Step::Mkdir(work.clone()),
        Step::Mkdir(cache.clone()),
        cmd(
            "tar",
            &[
                "-xzf",
                &s(&cache_file(super::plan::DL_PYTHON)),
                "-C",
                &s(&work),
            ],
        ),
        cmd_env(
            "bash",
            vec![s(&work.join("setup.sh"))],
            &[("AGENT_TOOLSDIRECTORY", s(cache))],
            Some(work.clone()),
        ),
    ];
    if let Some(req) = &cfg.python_seed.requirements_file {
        v.push(cmd(
            &s(&cfg.seed_python()),
            &["-m", "pip", "install", "-r", &s(req)],
        ));
    }
    v.extend([
        cmd("chown", &["-R", "root:root", &s(cache)]),
        cmd("chmod", &["-R", "go-w", &s(cache)]),
        cmd("chmod", &["-R", "a+rX", &s(cache)]),
        Step::WriteFile {
            path: PathBuf::from(STATE_DIR).join(SEED_MARKER),
            content: marker.into(),
            mode: 0o644,
        },
        Step::Remove(work),
    ]);
    v
}

pub fn execute(step: &Step) -> Result<(), String> {
    match step {
        Step::Cmd {
            program,
            args,
            env,
            cwd,
        } => {
            let mut c = std::process::Command::new(program);
            c.args(args).envs(env.iter().map(|(k, v)| (k, v)));
            if let Some(d) = cwd {
                c.current_dir(d);
            }
            let status = c.status().map_err(|e| format!("{program}: {e}"))?;
            if status.success() {
                Ok(())
            } else {
                Err(format!("{program} exited {status}"))
            }
        }
        Step::WriteFile {
            path,
            content,
            mode,
        } => write_file(path, content, *mode),
        Step::Mkdir(p) => {
            std::fs::create_dir_all(p).map_err(|e| format!("mkdir {}: {e}", p.display()))
        }
        Step::Symlink { target, link } => symlink(target, link),
        Step::Download {
            url,
            dest,
            sha256,
            size,
        } => super::download::fetch(url, dest, sha256, *size),
        Step::EnsureLine { path, line } => ensure_line(path, line),
        Step::Remove(p) => match std::fs::metadata(p) {
            Ok(m) if m.is_dir() => {
                std::fs::remove_dir_all(p).map_err(|e| format!("rm {}: {e}", p.display()))
            }
            Ok(_) => std::fs::remove_file(p).map_err(|e| format!("rm {}: {e}", p.display())),
            Err(_) => Ok(()),
        },
    }
}

fn ensure_line(path: &Path, line: &str) -> Result<(), String> {
    use std::io::Write;
    let have = std::fs::read_to_string(path).unwrap_or_default();
    if have.lines().any(|l| l.trim() == line) {
        return Ok(());
    }
    let mut f = std::fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(path)
        .map_err(|e| format!("open {}: {e}", path.display()))?;
    let sep = if have.is_empty() || have.ends_with('\n') {
        ""
    } else {
        "\n"
    };
    f.write_all(format!("{sep}{line}\n").as_bytes())
        .map_err(|e| format!("write {}: {e}", path.display()))
}

fn write_file(path: &Path, content: &str, mode: u32) -> Result<(), String> {
    use std::io::Write;
    let _ = std::fs::remove_file(path);
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut opts, mode);
    #[cfg(not(unix))]
    let _ = mode;
    let mut f = opts
        .open(path)
        .map_err(|e| format!("write {}: {e}", path.display()))?;
    f.write_all(content.as_bytes())
        .map_err(|e| format!("write {}: {e}", path.display()))
}

fn symlink(target: &Path, link: &Path) -> Result<(), String> {
    if std::fs::read_link(link).is_ok_and(|t| t == target) {
        return Ok(());
    }
    if std::fs::symlink_metadata(link).is_ok() {
        return Err(format!(
            "{} exists and is not the expected symlink",
            link.display()
        ));
    }
    #[cfg(unix)]
    return std::os::unix::fs::symlink(target, link)
        .map_err(|e| format!("symlink {}: {e}", link.display()));
    #[cfg(not(unix))]
    Err("symlinks need a Linux host".into())
}

#[cfg(test)]
mod tests {
    use super::super::config::sample;
    use super::*;

    #[test]
    fn sudoers_is_validated_before_it_is_installed() {
        let steps = steps_for(
            &sample(),
            &Action::InstallSudoers {
                content: "x".into(),
            },
        );
        let programs: Vec<&str> = steps
            .iter()
            .filter_map(|st| match st {
                Step::Cmd { program, .. } => Some(program.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(programs, ["visudo", "install"]);
    }
    #[test]
    fn user_commands_run_through_runuser_with_explicit_env() {
        let cfg = sample();
        let steps = steps_for(
            &cfg,
            &Action::PlaywrightChromium {
                user: "runner1".into(),
            },
        );
        let Step::Cmd { program, args, .. } = steps.last().unwrap() else {
            panic!()
        };
        assert_eq!(program, "runuser");
        assert_eq!(&args[..4], ["-u", "runner1", "--", "env"]);
        let h = cfg.home("runner1");
        assert!(args.contains(&format!("HOME={}", h.display())));
        assert!(args.contains(&format!(
            "PLAYWRIGHT_BROWSERS_PATH={}/.cache/ms-playwright",
            h.display()
        )));
        assert!(args.ends_with(&[
            "playwright".to_string(),
            "install".into(),
            "chromium".into()
        ]));
    }
    #[test]
    fn python_seed_verifies_then_seals_read_only_and_installs_requirements() {
        let cfg = sample();
        let steps = steps_for(
            &cfg,
            &Action::SeedPython {
                marker: "m\n".into(),
            },
        );
        let pos = |f: &dyn Fn(&Step) -> bool| steps.iter().position(f).unwrap();
        let setup = pos(&|st| matches!(st, Step::Cmd { program, .. } if program == "bash"));
        let pip =
            pos(&|st| matches!(st, Step::Cmd { args, .. } if args.contains(&"pip".to_string())));
        let chmod = pos(
            &|st| matches!(st, Step::Cmd { program, args, .. } if program == "chmod" && args[1] == "go-w"),
        );
        assert!(setup < pip && pip < chmod);
        assert!(
            matches!(&steps[setup], Step::Cmd { env, .. } if env == &[("AGENT_TOOLSDIRECTORY".to_string(), "/opt/hostedtoolcache".to_string())])
        );
    }
    #[test]
    fn hostedtoolcache_links_python_to_the_seed() {
        let steps = steps_for(
            &sample(),
            &Action::HostedToolcache {
                user: "runner2".into(),
            },
        );
        assert!(steps.contains(&Step::Symlink {
            target: "/opt/hostedtoolcache/Python".into(),
            link: sample().home("runner2").join("hostedtoolcache/Python")
        }));
    }
    #[test]
    fn no_step_goes_through_a_shell() {
        let cfg = sample();
        for a in [
            Action::AptInstall(vec!["git".into()]),
            Action::CreateUser("runner1".into()),
            Action::PlaywrightDeps,
        ] {
            for st in steps_for(&cfg, &a) {
                if let Step::Cmd { program, args, .. } = st {
                    assert!(
                        program != "sh" && program != "bash"
                            || args.first().is_some_and(|a| a.ends_with("setup.sh"))
                    );
                }
            }
        }
    }
    #[test]
    fn write_file_sets_mode_and_symlink_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let f = dir.path().join("f");
        write_file(&f, "x", 0o755).unwrap();
        write_file(&f, "y", 0o755).unwrap();
        assert_eq!(std::fs::read_to_string(&f).unwrap(), "y");
        #[cfg(unix)]
        assert_eq!(
            std::os::unix::fs::PermissionsExt::mode(&std::fs::metadata(&f).unwrap().permissions())
                & 0o777,
            0o755
        );
        #[cfg(unix)]
        {
            let l = dir.path().join("l");
            symlink(&f, &l).unwrap();
            symlink(&f, &l).unwrap();
            assert!(symlink(dir.path(), &l).is_err());
        }
    }
    #[test]
    fn ensure_line_appends_once() {
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("fstab");
        std::fs::write(&f, "a b c").unwrap();
        ensure_line(&f, "x y").unwrap();
        ensure_line(&f, "x y").unwrap();
        assert_eq!(std::fs::read_to_string(&f).unwrap(), "a b c\nx y\n");
    }
    #[test]
    fn hidepid_remounts_proc_and_persists_in_fstab() {
        let steps = steps_for(&sample(), &Action::HidepidProc);
        assert!(steps
            .iter()
            .any(|s| matches!(s, Step::EnsureLine { path, line }
            if path == Path::new("/etc/fstab") && line.contains("hidepid=2"))));
        assert!(steps
            .iter()
            .any(|s| matches!(s, Step::Cmd { program, args, .. }
            if program == "mount" && args.contains(&"remount,hidepid=2".to_string()))));
    }
    #[test]
    fn unpack_runner_writes_version_marker_last_so_failed_chown_leaves_no_marker() {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg = sample();
        cfg.home_base = dir.path().to_path_buf();
        let user = "plangraph_missing_user";
        let steps = steps_for(&cfg, &Action::UnpackRunner { user: user.into() });
        let chown_pos = steps
            .iter()
            .position(|st| matches!(st, Step::Cmd { program, .. } if program == "chown"))
            .unwrap();
        let marker_pos = steps
            .iter()
            .position(|st| {
                matches!(st, Step::WriteFile { path, .. } if path.ends_with(".plangraph-version"))
            })
            .unwrap();
        assert!(chown_pos < marker_pos);
        assert_eq!(marker_pos + 1, steps.len());

        let runner_dir = cfg.home(user).join("runner");
        execute(&Step::Mkdir(runner_dir.clone())).unwrap();
        let mut err = None;
        for step in &steps[chown_pos..] {
            if let Err(e) = execute(step) {
                err = Some(e);
                break;
            }
        }
        assert!(err.is_some(), "chown to missing user must fail");
        assert!(!runner_dir.join(".plangraph-version").exists());
    }
    #[test]
    fn create_user_passes_configured_home_for_non_default_home_base() {
        let mut cfg = sample();
        cfg.home_base = PathBuf::from("/srv/ci-runners");
        let steps = steps_for(&cfg, &Action::CreateUser("runner1".into()));
        assert_eq!(steps[0], Step::Mkdir(PathBuf::from("/srv/ci-runners")));
        assert_eq!(
            steps[1],
            Step::Cmd {
                program: "useradd".into(),
                args: vec![
                    "-m".into(),
                    "-d".into(),
                    "/srv/ci-runners/runner1".into(),
                    "-s".into(),
                    "/bin/bash".into(),
                    "runner1".into(),
                ],
                env: vec![],
                cwd: None,
            }
        );
    }
}
