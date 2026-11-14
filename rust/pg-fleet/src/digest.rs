use crate::digest_text::*;
use crate::fleet_name::{string, truth};
use crate::process::Output;
use chrono::{DateTime, Utc};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub trait Runner {
    fn run(&self, name: &str, args: &[String], cwd: Option<&Path>) -> Result<Output, String>;
}
pub struct Local;
impl Runner for Local {
    fn run(&self, name: &str, args: &[String], cwd: Option<&Path>) -> Result<Output, String> {
        let mut c = crate::process::command(name);
        c.args(args);
        if let Some(p) = cwd {
            c.current_dir(p);
        }
        if name == "git" {
            c.env("GIT_TERMINAL_PROMPT", "0");
        }
        if args.iter().any(|s| s == "plangraph.spawner.cli") {
            c.env("PYTHONPATH", "src")
                .env("PYTHONDONTWRITEBYTECODE", "1");
        }
        crate::process::capture(&mut c, if name == "git" { 10 } else { 30 })
    }
}

#[derive(Clone)]
pub struct Config {
    pub home: PathBuf,
    pub routing: PathBuf,
    pub fleet: PathBuf,
    pub data: PathBuf,
    pub snapshot: PathBuf,
    pub spawner: PathBuf,
    pub projects: PathBuf,
    pub dlog: PathBuf,
    pub cwd: PathBuf,
    pub sid: String,
    pub now: DateTime<Utc>,
    pub principal: Option<String>,
    pub handoff_cap: Option<String>,
    pub hard_cap: Option<String>,
}
impl Config {
    pub fn isolated(root: &Path) -> Self {
        Self {
            home: root.join("home"),
            routing: root.join("routing"),
            fleet: root.join("fleet"),
            data: root.join("data"),
            snapshot: root.join("snapshot.json"),
            spawner: root.join("spawner"),
            projects: root.join("home/.claude/projects"),
            dlog: root.join("decisions-log.md"),
            cwd: root.to_owned(),
            sid: String::new(),
            now: to_dt("2026-10-31T08:00:00Z").unwrap(),
            principal: None,
            handoff_cap: None,
            hard_cap: None,
        }
    }
    pub fn environment() -> Self {
        let envpath = |name, default: PathBuf| {
            std::env::var_os(name)
                .filter(|s| !s.is_empty())
                .map_or(default, Into::into)
        };
        let home = crate::home();
        let data = crate::data_home();
        Self {
            routing: crate::fleet_name::routing_state(),
            fleet: envpath("FLEET_STATE_DIR", home.join(".local/state/fleet")),
            snapshot: envpath(
                "RESUME_DIGEST_SNAPSHOT",
                home.join(".local/share/plangraph/plan.snapshot.json"),
            ),
            spawner: envpath(
                "RESUME_DIGEST_SPAWNER_DIR",
                home.join("code/plangraph-worktrees/spawner-main2"),
            ),
            projects: envpath("RESUME_DIGEST_PROJECTS", home.join(".claude/projects")),
            dlog: envpath("RESUME_DIGEST_DLOG", data.join("docs/decisions-log.md")),
            cwd: std::env::current_dir().unwrap_or_else(|_| ".".into()),
            sid: std::env::var("CLAUDE_CODE_SESSION_ID").unwrap_or_default(),
            now: std::env::var("RESUME_DIGEST_NOW")
                .ok()
                .and_then(|s| to_dt(&s))
                .unwrap_or_else(Utc::now),
            principal: None,
            handoff_cap: std::env::var("AUTOOS_CTX_HANDOFF").ok(),
            hard_cap: std::env::var("AUTOOS_CTX_HARD").ok(),
            home,
            data,
        }
    }
}

pub struct State {
    pub config: Config,
    pub specs: BTreeMap<String, Value>,
    pub rows: Vec<Value>,
    pub rows_note: Option<String>,
    pub role: String,
    pub how: String,
    pub spec: Value,
    pub session: String,
    pub session_how: String,
    pub role_cwd: PathBuf,
    pub card_text: String,
    pub card_ts: Option<DateTime<Utc>>,
    pub handoff: Option<PathBuf>,
    pub held: Vec<Value>,
    pub drill: Vec<String>,
}
impl State {
    pub fn fixture(config: Config, role: &str) -> Self {
        let role_cwd = config.cwd.clone();
        Self {
            config,
            specs: BTreeMap::new(),
            rows: vec![],
            rows_note: None,
            role: role.into(),
            how: "--role".into(),
            spec: Value::Null,
            session: role.into(),
            session_how: "role only".into(),
            role_cwd,
            card_text: String::new(),
            card_ts: None,
            handoff: None,
            held: vec![],
            drill: vec![],
        }
    }
    pub fn mine(&self, name: &str) -> bool {
        !self.role.is_empty() && role(name) == self.role
    }
    pub fn load(mut config: Config, arg: Option<&str>, runner: &dyn Runner) -> Self {
        // whoami is identity only. The snapshot is read once by sec_keeper;
        // no per-item show calls, paged listings, or role-derived principals.
        if config.principal.is_none() {
            config.principal = runner
                .run("kc", &["--json".into(), "whoami".into()], None)
                .ok()
                .filter(|o| o.code == 0)
                .and_then(|o| serde_json::from_str::<Value>(&o.stdout).ok())
                .and_then(|v| {
                    v["principal"]
                        .as_str()
                        .map(str::to_owned)
                        .or_else(|| v["principal"]["name"].as_str().map(str::to_owned))
                        .or_else(|| v["name"].as_str().map(str::to_owned))
                });
        }
        let mut s = Self::fixture(config, "");
        for p in glob(&s.config.routing.join("launch"), ".json") {
            if let Some(v) = crate::read_json(&p, 64 << 20).filter(Value::is_object) {
                s.specs
                    .insert(p.file_stem().unwrap().to_string_lossy().into_owned(), v);
            }
        }
        let rows = runner.run("claude", &["agents".into(), "--json".into()], None);
        let data = rows
            .as_ref()
            .ok()
            .filter(|o| o.code == 0)
            .and_then(|o| serde_json::from_str::<Value>(&o.stdout).ok());
        if let Some(Value::Array(a)) = data {
            s.rows = a
                .into_iter()
                .filter(|r| {
                    r.is_object()
                        && ["name", "sessionId", "cwd"]
                            .iter()
                            .all(|k| r[*k].is_null() || r[*k].is_string())
                })
                .collect();
        } else {
            s.rows_note = Some(format!(
                "claude agents --json unavailable{}",
                rows.err().map_or(String::new(), |e| format!(" ({e})"))
            ));
        }
        s.resolve(arg);
        s
    }
    pub fn resolve(&mut self, arg: Option<&str>) {
        if let Some(r) = arg {
            self.role = role(r);
            self.how = "--role".into();
        } else if !self.config.sid.is_empty() {
            if let Some((stem, v)) = self
                .specs
                .iter()
                .find(|(_, v)| string(v, "session_id") == self.config.sid)
            {
                self.role = role(v["name"].as_str().filter(|s| !s.is_empty()).unwrap_or(stem));
                self.how = "launch record".into();
            } else if let Some(r) = self.rows.iter().find(|r| {
                truth(&r["status"])
                    && string(r, "sessionId") == self.config.sid
                    && safe(string(r, "name"))
            }) {
                self.role = role(string(r, "name"));
                self.how = "live session".into();
            }
        }
        if self.role.is_empty() && arg.is_none() {
            let mut here: Vec<_> = self
                .specs
                .iter()
                .filter(|(_, v)| same_dir(string(v, "cwd"), &self.config.cwd.to_string_lossy()))
                .map(|(k, _)| k.clone())
                .collect();
            if here.len() > 1 {
                let narrowed: Vec<_> = here
                    .iter()
                    .filter(|h| {
                        self.rows.iter().any(|r| {
                            truth(&r["status"])
                                && same_dir(string(r, "cwd"), &self.config.cwd.to_string_lossy())
                                && role(string(r, "name")) == role(h)
                        })
                    })
                    .cloned()
                    .collect();
                if !narrowed.is_empty() {
                    here = narrowed;
                }
            }
            if here.len() == 1 {
                self.role = role(&here[0]);
                self.how = "cwd".into();
            } else {
                self.how = if here.is_empty() {
                    "no launch record owns this cwd".into()
                } else {
                    format!(
                        "cwd shared by {}",
                        here.into_iter().take(4).collect::<Vec<_>>().join(", ")
                    )
                };
            }
        }
        if !self.role.is_empty() && !safe(&self.role) {
            self.role.clear();
            self.how = "resolution failed (ValueError)".into();
        }
        self.spec = self.specs.get(&self.role).cloned().unwrap_or(Value::Null);
        self.role_cwd = self.spec["cwd"]
            .as_str()
            .filter(|s| !s.is_empty())
            .map_or_else(|| self.config.cwd.clone(), Into::into);
        let own = self.rows.iter().find(|r| {
            truth(&r["status"])
                && !self.config.sid.is_empty()
                && string(r, "sessionId") == self.config.sid
                && (self.mine(string(r, "name")) || self.role.is_empty())
        });
        let name = own.map(|r| string(r, "name").to_owned()).or_else(|| {
            if self.role.is_empty() {
                None
            } else {
                crate::fleet_name::live_name(&self.role, &self.rows)
            }
        });
        if let Some(name) = name {
            self.session = name;
            self.session_how = "live".into();
        } else if let Some(name) = self.spec["session_name"].as_str().filter(|s| !s.is_empty()) {
            self.session = name.into();
            self.session_how = "launch record".into();
        } else {
            self.session = if self.role.is_empty() {
                "unknown".into()
            } else {
                self.role.clone()
            };
            self.session_how = "role only".into();
        }
    }
}

fn cap(v: Option<&str>) -> String {
    v.map_or("unset".into(), |s| {
        s.parse::<i64>().map_or_else(
            |_| cut(s, 12),
            |n| {
                if n % 1000 == 0 {
                    format!("{}k", n / 1000)
                } else {
                    n.to_string()
                }
            },
        )
    })
}
pub fn identity(s: &State) -> Vec<String> {
    let who = if s.role.is_empty() {
        format!("role unknown [{}; pass --role]", s.how)
    } else {
        format!("role {} [{}]", s.role, s.how)
    };
    let mut where_ = format!("cwd {}", s.config.cwd.display());
    if s.role_cwd != s.config.cwd {
        where_ += &format!(" | role cwd {}", s.role_cwd.display());
    }
    let mut out = vec![
        format!("== Identity {}", stamp(s.config.now)),
        format!(
            "{who} | session {} [{}] | {where_}",
            s.session, s.session_how
        ),
        format!(
            "caps: handoff {}, hard {} (AUTOOS_CTX_HANDOFF/HARD)",
            cap(s.config.handoff_cap.as_deref()),
            cap(s.config.hard_cap.as_deref())
        ),
        format!(
            "measured context {}",
            crate::digest_sources::context_tokens(s).map_or("unknown".into(), |n| n.to_string())
        ),
    ];
    if let Some(note) = &s.rows_note {
        out.push(format!("note: {note}"));
    }
    out
}

pub fn build(config: Config, arg: Option<&str>, runner: &dyn Runner) -> Vec<String> {
    let mut s = State::load(config, arg, runner);
    let mut out = vec![];
    out.extend(fit(identity(&s), 5));
    out.extend(fit(crate::digest_card::section(&mut s), 43));
    out.extend(fit(crate::digest_keeper::section(&mut s), 20));
    out.extend(fit(crate::digest_sources::operator(&s), 15));
    out.extend(fit(crate::digest_sources::spawner(&s, runner), 8));
    out.extend(fit(crate::digest_git::section(&s, runner), 14));
    out.extend(fit(crate::digest_sources::inbox(&s), 6));
    out.extend(fit(crate::digest_sources::children(&s), 6));
    out = out
        .into_iter()
        .map(|ln| ln.chars().take(400).collect())
        .take(119)
        .collect();
    out.extend(crate::carried::section(&s));
    let where_ = s.handoff.as_ref().map_or("none known".into(), |p| {
        format!(
            "{}{}",
            p.display(),
            if usable(p) { "" } else { " [missing]" }
        )
    });
    out.push(format!(
        "Full handoff: {where_} (read only if this page is not enough)"
    ));
    out
}
pub fn main(args: &[String]) -> i32 {
    let opts = match crate::cli::options(args, &["--role"], &["--help", "-h"]) {
        Ok(o) => o,
        Err(e) => return crate::cli::error("resume-digest", &e),
    };
    if opts.contains_key("--help") || opts.contains_key("-h") {
        print!("{}", crate::cli::help("resume-digest"));
        return 0;
    }
    let arg = opts
        .get("--role")
        .map(String::as_str)
        .filter(|s| !s.is_empty());
    if let Some(arg) = arg.filter(|s| !safe(s)) {
        return crate::cli::error(
            "resume-digest",
            &format!("bad --role {}", crate::summary_sources::py_repr(arg)),
        );
    }
    println!("{}", build(Config::environment(), arg, &Local).join("\n"));
    0
}
