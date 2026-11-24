#![allow(dead_code)]
//! Shared fixtures of the lifecycle test binaries.

use pg_session_resume::engine::Config;
use pg_session_resume::host::{Host, JobState};
use pg_session_resume::record::Record;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

pub const HEAD: &str = "0123456789abcdef0123456789abcdef01234567";
pub const HEAD2: &str = "fedcba9876543210fedcba9876543210fedcba98";
pub const SID: &str = "1591af3e-91d1-442a-bc81-f5dd71769181";

pub struct Fake {
    pub now: u64,
    pub mem: Vec<u64>,
    pub mem_reads: std::cell::Cell<usize>,
    pub activity: Option<u64>,
    pub dirty: Option<bool>,
    pub head: Option<String>,
    pub runs: Vec<(Option<PathBuf>, Vec<String>)>,
    pub spawns: Vec<Vec<String>>,
    pub lines: Vec<String>,
    pub fail_run: bool,
    pub job: Option<JobState>,
    pub procs: HashMap<u32, u64>,
    pub next_pid: u32,
    pub children: Option<bool>,
    pub wt_id: Option<String>,
    pub turn_ended: Option<bool>,
    pub tlen: Option<u64>,
    pub tcontains: Option<bool>,
    pub find: Option<(u32, u64)>,
    /// A spawned process that is gone again when it is looked at.
    pub spawn_dies: bool,
    /// Whether a live process carries the prompt in its arguments.
    pub holds_prompt: bool,
    pub launched: HashMap<u32, u64>,
}

impl Fake {
    pub fn new(now: u64) -> Fake {
        Fake {
            now,
            mem: vec![8000, 9000],
            mem_reads: Default::default(),
            activity: None,
            dirty: Some(false),
            head: Some(HEAD.into()),
            runs: vec![],
            spawns: vec![],
            lines: vec![],
            fail_run: false,
            job: Some(JobState {
                state: "done".into(),
                tasks: 0,
            }),
            procs: HashMap::new(),
            next_pid: 777,
            children: Some(false),
            wt_id: Some("w1".into()),
            turn_ended: Some(true),
            tlen: Some(100),
            tcontains: Some(true),
            find: None,
            spawn_dies: false,
            holds_prompt: true,
            launched: HashMap::new(),
        }
    }
}

impl Host for Fake {
    fn now(&self) -> u64 {
        self.now
    }
    fn run(&mut self, cwd: Option<&Path>, argv: &[String]) -> Result<String, String> {
        self.runs.push((cwd.map(Path::to_path_buf), argv.to_vec()));
        if self.fail_run {
            Err("boom".into())
        } else {
            Ok(String::new())
        }
    }
    fn spawn(&mut self, _: &Path, argv: &[String]) -> Result<u32, String> {
        self.spawns.push(argv.to_vec());
        if self.fail_run {
            return Err("boom".into());
        }
        self.launched.insert(self.next_pid, 55);
        if !self.spawn_dies {
            self.procs.insert(self.next_pid, 55);
        }
        Ok(self.next_pid)
    }
    fn proc_start(&self, pid: u32) -> Option<u64> {
        self.procs.get(&pid).copied()
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
        self.turn_ended
    }
    fn transcript_len(&self, _: &Record) -> Option<u64> {
        self.tlen
    }
    fn transcript_contains(&self, _: &Record, _: u64, _: &str) -> Option<bool> {
        self.tcontains
    }
    fn process_holds_prompt(&self, pid: u32, start: u64, _: &str) -> bool {
        self.holds_prompt && self.procs.get(&pid) == Some(&start)
    }
    fn launch_start(&self, pid: u32) -> Option<u64> {
        self.launched.get(&pid).copied()
    }
    fn find_process(&self, _: &str) -> Option<(u32, u64)> {
        self.find
    }
    fn mem_available_mb(&self) -> Option<u64> {
        let i = self.mem_reads.get();
        self.mem_reads.set(i + 1);
        self.mem.get(i).or(self.mem.last()).copied()
    }
    fn git_head(&self, _: &Path) -> Option<String> {
        self.head.clone()
    }
    fn git_dirty(&self, _: &Path) -> Option<bool> {
        self.dirty
    }
    fn activity_ts(&self, _: &Record) -> Option<u64> {
        self.activity
    }
    fn log(&mut self, line: &str) {
        self.lines.push(line.to_owned());
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
        min_free_mb: 4096,
        allow: vec!["*-L1".into(), "plan-*".into()],
    };
    Rig { _dir: dir, cfg, wt }
}

pub fn record(r: &Rig, name: &str) -> Value {
    serde_json::from_slice(
        &std::fs::read(r.cfg.launch_dir.join(format!("{name}-0644.json"))).unwrap(),
    )
    .unwrap()
}

/// A findings round gets its reviewed-head sidecar (HEAD) the way the contract says: first.
pub fn write(r: &Rig, file: &str, text: &str) {
    if let Some(stem) = file
        .strip_suffix(".txt")
        .filter(|s| s.contains(".fix-round-"))
    {
        std::fs::write(r.cfg.pr_dir.join(format!("{stem}.head")), HEAD).unwrap();
    }
    std::fs::write(r.cfg.pr_dir.join(file), text).unwrap();
}

pub fn write_head(r: &Rig, round: u64, sha: &str) {
    std::fs::write(
        r.cfg.pr_dir.join(format!("lane-a.fix-round-{round}.head")),
        sha,
    )
    .unwrap();
}

pub fn touch(r: &Rig, file: &str, secs: u64) {
    let f = std::fs::File::options()
        .write(true)
        .open(r.cfg.pr_dir.join(file))
        .unwrap();
    f.set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_secs(secs))
        .unwrap();
}

/// The status file says DONE; its mtime (10_000) is the moment the review starts.
pub fn done(r: &Rig, head: &str) {
    write(
        r,
        "lane-a.status",
        &format!("DONE k abc\nlane-a DONE\nHEAD: {head}\nREVIEW: none\nVERDICT: ACCEPT\n"),
    );
    touch(r, "lane-a.status", 10_000);
}

/// A second launch record for the same lane (cwd names the lane).
pub fn add_record(r: &Rig, file: &str, extra: Value) {
    let mut rec = json!({"name": "writer", "session_name": file, "cwd": r.wt,
        "session_id": "2222222a-91d1-442a-bc81-f5dd71769181", "job_id": "2222222a", "ts": 2000});
    rec.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    std::fs::write(
        r.cfg.launch_dir.join(format!("{file}.json")),
        rec.to_string(),
    )
    .unwrap();
}

/// The log line a resume launch wrote (the acknowledgement line follows it).
pub fn resume_line(h: &Fake) -> &str {
    h.lines
        .iter()
        .find(|l| l.contains("event=resume name="))
        .map_or("", String::as_str)
}

/// Merge `patch` into the `resume` object of a launch record on disk.
pub fn patch_resume(r: &Rig, name: &str, patch: Value) {
    let path = r.cfg.launch_dir.join(format!("{name}-0644.json"));
    let mut rec: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    if rec.get("resume").is_none() {
        rec["resume"] = json!({});
    }
    for (k, v) in patch.as_object().unwrap() {
        rec["resume"][k] = v.clone();
    }
    std::fs::write(&path, rec.to_string()).unwrap();
}
