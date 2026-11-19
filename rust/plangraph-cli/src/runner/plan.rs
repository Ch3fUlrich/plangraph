//! Pure planner: observed host state -> ordered idempotent actions.
use super::config::Config;
use super::template;
use std::collections::BTreeMap;
use std::path::PathBuf;

pub const CACHE_DIR: &str = "/var/cache/plangraph-runner";
pub const STATE_DIR: &str = "/var/lib/plangraph-runner";
pub const SUDOERS_FILE: &str = "/etc/sudoers.d/ci-apt";
pub const DL_RUNNER: &str = "actions-runner";
pub const DL_PYTHON: &str = "python-seed";

pub fn cache_file(name: &str) -> PathBuf {
    PathBuf::from(CACHE_DIR).join(format!("{name}.tar.gz"))
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct UserState {
    pub name: String,
    pub exists: bool,
    pub lingering: bool,
    pub runner_version: Option<String>,
    pub hosted_cache: bool,
    pub playwright: bool,
    pub temporal_link: bool,
    /// A path the plan would link already exists with another target.
    pub link_conflict: Option<String>,
    pub run_one: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct State {
    pub apt_missing: Vec<String>,
    pub users: Vec<UserState>,
    pub sudoers: Option<String>,
    pub seed_marker: Option<String>,
    pub requirements_sha: Option<String>,
    pub playwright_deps: bool,
    /// /proc is mounted with hidepid=2 (or stricter).
    pub proc_hidepid: bool,
    /// Download name -> sha256 of the file already in the cache dir.
    pub cached: BTreeMap<String, String>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    AptInstall(Vec<String>),
    CreateUser(String),
    EnableLinger(String),
    InstallSudoers {
        content: String,
    },
    HidepidProc,
    Download {
        name: String,
        url: String,
        sha256: String,
        size: Option<u64>,
    },
    UnpackRunner {
        user: String,
    },
    SeedPython {
        marker: String,
    },
    HostedToolcache {
        user: String,
    },
    PlaywrightChromium {
        user: String,
    },
    PlaywrightDeps,
    TemporalLink {
        user: String,
    },
    RunOne {
        user: String,
        content: String,
    },
}

impl Action {
    pub fn describe(&self) -> String {
        match self {
            Action::AptInstall(p) => format!("apt install {}", p.join(" ")),
            Action::CreateUser(u) => format!("create user {u}"),
            Action::EnableLinger(u) => format!("enable lingering for {u}"),
            Action::InstallSudoers { .. } => format!("install {SUDOERS_FILE} (visudo checked)"),
            Action::Download { name, url, .. } => {
                format!("download {name} from {}, verify sha256", redact_url(url))
            }
            Action::HidepidProc => {
                format!("mount /proc with hidepid=2 ({FSTAB_LINE} in /etc/fstab)")
            }
            Action::UnpackRunner { user } => format!("unpack Actions runner for {user}"),
            Action::SeedPython { .. } => "seed Python tool cache (read-only, root-owned)".into(),
            Action::HostedToolcache { user } => format!("writable hostedtoolcache for {user}"),
            Action::PlaywrightChromium { user } => format!("playwright chromium for {user}"),
            Action::PlaywrightDeps => "playwright install-deps chromium".into(),
            Action::TemporalLink { user } => format!("temporal-cli symlink for {user}"),
            Action::RunOne { user, .. } => format!("write run-one.sh for {user}"),
        }
    }
}

pub const FSTAB_LINE: &str = "proc /proc proc defaults,hidepid=2 0 0";

/// Host and path only: userinfo and query strings can carry credentials.
pub fn redact_url(url: &str) -> String {
    let rest = url.split_once("://").map_or(url, |(_, r)| r);
    let rest = rest.split(['?', '#']).next().unwrap_or("");
    let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
    let host = authority.rsplit('@').next().unwrap_or("");
    format!("{host}/{path}")
}

pub fn seed_marker(cfg: &Config, requirements_sha: &str) -> String {
    let ps = &cfg.python_seed;
    format!(
        "{} {requirements_sha} {} {}\n",
        ps.sha256,
        ps.version,
        ps.tool_cache.display()
    )
}

fn needs_download(
    state: &State,
    name: &str,
    sha: &str,
    url: &str,
    size: Option<u64>,
) -> Result<Option<Action>, String> {
    match state.cached.get(name) {
        Some(have) if have == sha => Ok(None),
        Some(have) => Err(format!(
            "refuse: cached {name} has sha256 {have}, config pins {sha}; remove {} or fix the config",
            cache_file(name).display()
        )),
        None => Ok(Some(Action::Download {
            name: name.into(),
            url: url.into(),
            sha256: sha.into(),
            size,
        })),
    }
}

pub fn plan(cfg: &Config, state: &State) -> Result<Vec<Action>, String> {
    let mut out = Vec::new();
    let names: Vec<String> = state.users.iter().map(|u| u.name.clone()).collect();
    if !state.apt_missing.is_empty() {
        out.push(Action::AptInstall(state.apt_missing.clone()));
    }
    for u in &state.users {
        if !u.exists {
            out.push(Action::CreateUser(u.name.clone()));
        }
        if !u.lingering {
            out.push(Action::EnableLinger(u.name.clone()));
        }
    }
    let sudoers = template::sudoers(&names);
    if state.sudoers.as_deref() != Some(sudoers.as_str()) {
        out.push(Action::InstallSudoers { content: sudoers });
    }
    if !state.proc_hidepid {
        out.push(Action::HidepidProc);
    }
    if let Some(c) = state.users.iter().find_map(|u| u.link_conflict.as_ref()) {
        return Err(format!(
            "refuse: {c} exists with a different target; remove it or fix the config"
        ));
    }
    let ar = &cfg.actions_runner;
    let stale_runner = |u: &UserState| u.runner_version.as_deref() != Some(ar.version.as_str());
    if state.users.iter().any(stale_runner) {
        out.extend(needs_download(state, DL_RUNNER, &ar.sha256, &ar.url, None)?);
        for u in state.users.iter().filter(|u| stale_runner(u)) {
            out.push(Action::UnpackRunner {
                user: u.name.clone(),
            });
        }
    }
    let req_sha = match (&cfg.python_seed.requirements_file, &state.requirements_sha) {
        (Some(path), None) => {
            return Err(format!("requirements file {} unreadable", path.display()))
        }
        (_, sha) => sha.clone().unwrap_or_else(|| "-".into()),
    };
    let marker = seed_marker(cfg, &req_sha);
    if state.seed_marker.as_deref() != Some(marker.as_str()) {
        let ps = &cfg.python_seed;
        out.extend(needs_download(
            state,
            DL_PYTHON,
            &ps.sha256,
            &ps.url,
            Some(ps.size),
        )?);
        out.push(Action::SeedPython { marker });
    }
    let run_one = template::run_one(&cfg.temporal_test_address);
    for u in &state.users {
        let user = u.name.clone();
        if !u.hosted_cache {
            out.push(Action::HostedToolcache { user: user.clone() });
        }
        if !u.playwright {
            out.push(Action::PlaywrightChromium { user: user.clone() });
        }
        if !u.temporal_link {
            out.push(Action::TemporalLink { user: user.clone() });
        }
        if u.run_one.as_deref() != Some(run_one.as_str()) {
            out.push(Action::RunOne {
                user,
                content: run_one.clone(),
            });
        }
    }
    if !state.playwright_deps {
        out.push(Action::PlaywrightDeps);
    }
    Ok(out)
}

#[cfg(test)]
pub(crate) fn complete_state(cfg: &Config, count: u32) -> State {
    let run_one = template::run_one(&cfg.temporal_test_address);
    let names = cfg.user_names(count);
    State {
        apt_missing: vec![],
        sudoers: Some(template::sudoers(&names)),
        seed_marker: Some(seed_marker(cfg, "reqsha")),
        requirements_sha: Some("reqsha".into()),
        playwright_deps: true,
        proc_hidepid: true,
        cached: BTreeMap::new(),
        users: names
            .into_iter()
            .map(|name| UserState {
                name,
                exists: true,
                lingering: true,
                runner_version: Some(cfg.actions_runner.version.clone()),
                hosted_cache: true,
                playwright: true,
                temporal_link: true,
                link_conflict: None,
                run_one: Some(run_one.clone()),
            })
            .collect(),
    }
}

#[cfg(test)]
pub(crate) fn fresh_state(cfg: &Config, count: u32) -> State {
    State {
        apt_missing: cfg.apt_packages.clone(),
        users: cfg
            .user_names(count)
            .into_iter()
            .map(|name| UserState {
                name,
                ..Default::default()
            })
            .collect(),
        requirements_sha: Some("reqsha".into()),
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::super::config::sample;
    use super::*;

    #[test]
    fn fully_set_up_host_has_empty_plan() {
        let cfg = sample();
        assert_eq!(plan(&cfg, &complete_state(&cfg, 2)).unwrap(), vec![]);
    }
    #[test]
    fn fresh_host_gets_the_full_ordered_plan() {
        let cfg = sample();
        let acts = plan(&cfg, &fresh_state(&cfg, 2)).unwrap();
        let d: Vec<String> = acts.iter().map(Action::describe).collect();
        assert!(d[0].starts_with("apt install protobuf-compiler libprotobuf-dev"));
        let pos = |needle: &str| d.iter().position(|s| s.contains(needle)).unwrap();
        assert!(pos("create user runner1") < pos("enable lingering for runner1"));
        assert!(pos("sudoers") < pos("download actions-runner"));
        assert!(pos("download python-seed") < pos("seed Python"));
        assert!(pos("seed Python") < pos("playwright chromium for runner1"));
        assert!(pos("playwright chromium for runner2") < pos("install-deps"));
        assert_eq!(
            acts.iter()
                .filter(|a| matches!(a, Action::RunOne { .. }))
                .count(),
            2
        );
        assert_eq!(acts.len(), 1 + 4 + 1 + 1 + 1 + 2 + 2 + 8 + 1);
        assert_eq!(plan(&cfg, &fresh_state(&cfg, 2)).unwrap(), acts);
    }
    #[test]
    fn partial_state_plans_only_the_gaps() {
        let cfg = sample();
        let mut s = complete_state(&cfg, 3);
        s.apt_missing = vec!["libprotobuf-dev".into()];
        s.users[1].playwright = false;
        s.users[2].run_one = Some("old".into());
        s.users[2].lingering = false;
        let acts = plan(&cfg, &s).unwrap();
        assert_eq!(
            acts,
            vec![
                Action::AptInstall(vec!["libprotobuf-dev".into()]),
                Action::EnableLinger("runner3".into()),
                Action::PlaywrightChromium {
                    user: "runner2".into()
                },
                Action::RunOne {
                    user: "runner3".into(),
                    content: template::run_one(&cfg.temporal_test_address)
                },
            ]
        );
    }
    #[test]
    fn growing_the_count_adds_only_the_new_user() {
        let cfg = sample();
        let mut s = complete_state(&cfg, 1);
        s.users.push(UserState {
            name: "runner2".into(),
            ..Default::default()
        });
        s.sudoers = Some(template::sudoers(&["runner1".into()]));
        let acts = plan(&cfg, &s).unwrap();
        assert!(acts
            .iter()
            .any(|a| *a == Action::CreateUser("runner2".into())));
        assert!(!acts
            .iter()
            .any(|a| matches!(a, Action::CreateUser(u) if u == "runner1")));
        assert!(acts
            .iter()
            .any(|a| matches!(a, Action::InstallSudoers { .. })));
    }
    #[test]
    fn checksum_mismatch_in_cache_is_refused_and_match_skips_download() {
        let cfg = sample();
        let mut s = fresh_state(&cfg, 1);
        s.cached.insert(DL_RUNNER.into(), "f".repeat(64));
        let err = plan(&cfg, &s).unwrap_err();
        assert!(err.starts_with("refuse:") && err.contains(DL_RUNNER));
        s.cached
            .insert(DL_RUNNER.into(), cfg.actions_runner.sha256.clone());
        let acts = plan(&cfg, &s).unwrap();
        assert!(!acts
            .iter()
            .any(|a| matches!(a, Action::Download { name, .. } if name == DL_RUNNER)));
    }
    #[test]
    fn changed_requirements_reseed_and_missing_file_is_an_error() {
        let cfg = sample();
        let mut s = complete_state(&cfg, 1);
        s.requirements_sha = Some("newsha".into());
        let acts = plan(&cfg, &s).unwrap();
        assert!(matches!(acts.last(), Some(Action::SeedPython { .. })));
        s.requirements_sha = None;
        assert!(plan(&cfg, &s).unwrap_err().contains("unreadable"));
    }
    #[test]
    fn hidepid_is_planned_only_when_missing() {
        let cfg = sample();
        let mut s = complete_state(&cfg, 1);
        s.proc_hidepid = false;
        assert_eq!(plan(&cfg, &s).unwrap(), vec![Action::HidepidProc]);
    }
    #[test]
    fn conflicting_tool_cache_link_is_refused_at_plan_time() {
        let cfg = sample();
        let mut s = complete_state(&cfg, 1);
        s.users[0].link_conflict = Some("/home/runner1/hostedtoolcache/Python".into());
        let err = plan(&cfg, &s).unwrap_err();
        assert!(err.starts_with("refuse:") && err.contains("hostedtoolcache/Python"));
    }
    #[test]
    fn seed_marker_tracks_version_and_tool_cache() {
        let a = sample();
        let mut b = sample();
        b.python_seed.tool_cache = "/elsewhere".into();
        assert_ne!(seed_marker(&a, "r"), seed_marker(&b, "r"));
        let mut c = sample();
        c.python_seed.version.push('1');
        assert_ne!(seed_marker(&a, "r"), seed_marker(&c, "r"));
    }
    #[test]
    fn download_urls_print_host_and_path_only() {
        assert_eq!(
            redact_url("https://user:pw@example.com/a/b.tgz?sig=secret#f"),
            "example.com/a/b.tgz"
        );
        let a = Action::Download {
            name: "n".into(),
            url: "https://h.example/p/x.tgz?token=abc".into(),
            sha256: "s".into(),
            size: None,
        };
        let d = a.describe();
        assert!(d.contains("h.example/p/x.tgz") && !d.contains("token"));
    }
}
