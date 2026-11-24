#![allow(dead_code)]
//! Shared fixtures of the lifecycle test binaries.

use pg_session_resume::engine::Config;
use pg_session_resume::host::{Host, JobState, Process};
use pg_session_resume::record::Record;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

pub const HEAD: &str = "0123456789abcdef0123456789abcdef01234567";
pub const SID: &str = "1591af3e-91d1-442a-bc81-f5dd71769181";

pub struct Fake {
    pub now: u64,
    pub mem: Vec<u64>,
    pub mem_reads: std::cell::Cell<usize>,
    pub activity: Option<u64>,
    pub head: Option<String>,
    pub runs: Vec<(Option<PathBuf>, Vec<String>)>,
    pub lines: Vec<String>,
    pub fail_run: bool,
    pub job: Option<JobState>,
    /// pid -> start time of the live processes.
    pub procs: HashMap<u32, u64>,
    /// Every pid is unreadable (the /proc entry cannot be parsed).
    pub proc_unknown: bool,
    /// The signalled process does not exit.
    pub survives: bool,
    pub terminated: Vec<(u32, u64)>,
    /// The log cannot be appended to.
    pub log_fails: bool,
    /// Created as a directory while a stop command runs, so the record's next save fails.
    pub block_on_run: Option<PathBuf>,
    /// While a stop command runs this path becomes a non-empty directory (a file that can no
    /// longer be removed).
    pub jam_on_run: Option<PathBuf>,
    /// The session starts a turn while its worktree is looked up.
    pub busy_on_head: bool,
    pub busy: std::cell::Cell<bool>,
    /// The session shows this activity once its worktree has been looked up.
    pub active_after_head: Option<u64>,
    pub fresh_activity: std::cell::Cell<Option<u64>>,
    pub children: Option<bool>,
    pub wt_id: Option<String>,
    pub turn_ended: Option<bool>,
}

impl Fake {
    pub fn new(now: u64) -> Fake {
        Fake {
            now,
            mem: vec![8000, 9000],
            mem_reads: Default::default(),
            activity: None,
            head: Some(HEAD.into()),
            runs: vec![],
            lines: vec![],
            fail_run: false,
            job: Some(JobState {
                state: "done".into(),
                tasks: Some(0),
            }),
            procs: HashMap::new(),
            proc_unknown: false,
            survives: false,
            terminated: vec![],
            log_fails: false,
            block_on_run: None,
            jam_on_run: None,
            busy_on_head: false,
            busy: Default::default(),
            active_after_head: None,
            fresh_activity: Default::default(),
            children: Some(false),
            wt_id: Some("w1".into()),
            turn_ended: Some(true),
        }
    }
}

impl Host for Fake {
    fn now(&self) -> u64 {
        self.now
    }
    fn run(&mut self, cwd: Option<&Path>, argv: &[String]) -> Result<String, String> {
        self.runs.push((cwd.map(Path::to_path_buf), argv.to_vec()));
        if let Some(p) = &self.block_on_run {
            std::fs::create_dir(p).unwrap();
        }
        if let Some(p) = &self.jam_on_run {
            let _ = std::fs::remove_file(p);
            std::fs::create_dir(p).unwrap();
            std::fs::write(p.join("x"), "x").unwrap();
        }
        if self.fail_run {
            Err("boom".into())
        } else {
            Ok(String::new())
        }
    }
    fn process(&self, pid: u32) -> Process {
        match (self.proc_unknown, self.procs.get(&pid)) {
            (true, _) => Process::Unknown,
            (_, Some(s)) => Process::Live(*s),
            (_, None) => Process::Gone,
        }
    }
    fn terminate(&mut self, pid: u32, start: u64) -> Result<(), String> {
        self.terminated.push((pid, start));
        if self.fail_run || self.survives {
            return Err("still running".into());
        }
        self.procs.remove(&pid);
        Ok(())
    }
    fn job_state(&self, _: &Record) -> Option<JobState> {
        self.job.clone()
    }
    fn has_children(&self, _: u32) -> Option<bool> {
        self.children
    }
    fn worktree_id(&self, _: &Path) -> Option<String> {
        self.wt_id.clone()
    }
    fn turn_ended(&self, _: &Record) -> Option<bool> {
        if self.busy.get() {
            return Some(false);
        }
        self.turn_ended
    }
    fn mem_available_mb(&self) -> Option<u64> {
        let i = self.mem_reads.get();
        self.mem_reads.set(i + 1);
        self.mem.get(i).or(self.mem.last()).copied()
    }
    fn git_head(&self, _: &Path) -> Option<String> {
        self.busy.set(self.busy_on_head);
        self.fresh_activity.set(self.active_after_head);
        self.head.clone()
    }
    fn activity_ts(&self, _: &Record) -> Option<u64> {
        self.fresh_activity.get().or(self.activity)
    }
    fn log(&mut self, line: &str) -> bool {
        if self.log_fails {
            return false;
        }
        self.lines.push(line.to_owned());
        true
    }
}

pub struct Rig {
    pub _dir: tempfile::TempDir,
    pub cfg: Config,
    pub wt: PathBuf,
}

pub fn rig(name: &str, extra: Value) -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let (launch, pr, wt) = (
        dir.path().join("sessions"),
        dir.path().join("pr"),
        dir.path().join("lane-a"),
    );
    for d in [&launch, &pr, &wt] {
        std::fs::create_dir_all(d).unwrap();
    }
    let mut rec = json!({"name": name, "session_name": format!("{name}-0644"), "cwd": wt,
        "session_id": SID, "job_id": &SID[..8], "ts": 1000, "model": "sonnet"});
    rec.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    std::fs::write(launch.join(format!("{name}-0644.json")), rec.to_string()).unwrap();
    let cfg = Config {
        launch_dir: launch,
        pr_dir: pr,
        idle_s: 1800,
        allow: vec!["*-L1".into(), "*-L1-*".into(), "plan-*".into()],
    };
    Rig { _dir: dir, cfg, wt }
}

pub fn record(r: &Rig, name: &str) -> Value {
    serde_json::from_slice(
        &std::fs::read(r.cfg.launch_dir.join(format!("{name}-0644.json"))).unwrap(),
    )
    .unwrap()
}

/// A lane file for the lane every rig uses.
pub fn write(r: &Rig, file: &str, text: &str) {
    std::fs::write(r.cfg.pr_dir.join(file), text).unwrap();
}

/// The status file says DONE with `head`.
pub fn done(r: &Rig, head: &str) {
    write(
        r,
        "lane-a.status",
        &format!("DONE k abc\nlane-a DONE\nHEAD: {head}\nREVIEW: none\nVERDICT: ACCEPT\n"),
    );
}

pub fn read(path: &std::path::Path) -> Value {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

pub fn lane_copy(r: &Rig) -> Value {
    read(&r.cfg.pr_dir.join("lane-a.resume.json"))
}

pub fn pending(r: &Rig) -> std::path::PathBuf {
    r.cfg.launch_dir.join("writer-0644.pending")
}

pub fn stopped_job() -> Option<JobState> {
    Some(JobState {
        state: "stopped".into(),
        tasks: Some(0),
    })
}
