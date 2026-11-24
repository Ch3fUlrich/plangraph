//! SESSION-AUTO-CLOSE done-when: DONE stops a session with its handle recorded; a
//! findings file resumes the same session id with exactly the file text; idle
//! sessions are stopped; the allowlist is never stopped; one log line per event.

use pg_session_resume::engine::{reap_idle, resume_findings, stop_done, Config};
use pg_session_resume::host::{Host, JobState};
use pg_session_resume::record::Record;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

const HEAD: &str = "0123456789abcdef0123456789abcdef01234567";
const HEAD2: &str = "fedcba9876543210fedcba9876543210fedcba98";
const SID: &str = "1591af3e-91d1-442a-bc81-f5dd71769181";

struct Fake {
    now: u64,
    mem: Vec<u64>,
    mem_reads: std::cell::Cell<usize>,
    activity: Option<u64>,
    dirty: Option<bool>,
    head: Option<String>,
    runs: Vec<(Option<PathBuf>, Vec<String>)>,
    spawns: Vec<Vec<String>>,
    lines: Vec<String>,
    fail_run: bool,
    job: Option<JobState>,
    procs: HashMap<u32, u64>,
    next_pid: u32,
}

impl Fake {
    fn new(now: u64) -> Fake {
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
        self.procs.insert(self.next_pid, 55);
        Ok(self.next_pid)
    }
    fn proc_start(&self, pid: u32) -> Option<u64> {
        self.procs.get(&pid).copied()
    }
    fn job_state(&self, _: &Record) -> Option<JobState> {
        self.job.clone()
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

struct Rig {
    _dir: tempfile::TempDir,
    cfg: Config,
    wt: PathBuf,
}

fn rig(name: &str, extra: Value) -> Rig {
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

fn record(r: &Rig, name: &str) -> Value {
    serde_json::from_slice(
        &std::fs::read(r.cfg.launch_dir.join(format!("{name}-0644.json"))).unwrap(),
    )
    .unwrap()
}

fn write(r: &Rig, file: &str, text: &str) {
    std::fs::write(r.cfg.pr_dir.join(file), text).unwrap();
}

fn touch(r: &Rig, file: &str, secs: u64) {
    let f = std::fs::File::options()
        .write(true)
        .open(r.cfg.pr_dir.join(file))
        .unwrap();
    f.set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_secs(secs))
        .unwrap();
}

/// The status file says DONE; its mtime (10_000) is the moment the review starts.
fn done(r: &Rig, head: &str) {
    write(
        r,
        "lane-a.status",
        &format!("DONE k abc\nlane-a DONE\nHEAD: {head}\nREVIEW: none\nVERDICT: ACCEPT\n"),
    );
    touch(r, "lane-a.status", 10_000);
}

/// A second launch record for the same lane (cwd names the lane).
fn add_record(r: &Rig, file: &str, extra: Value) {
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

#[test]
fn done_stops_the_session_and_records_the_resume_handle() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(h.runs[0].1, ["claude", "stop", "1591af3e"]);
    let rec = record(&r, "writer");
    assert_eq!(rec["state"], "stopped");
    assert_eq!(rec["stopped_ts"], 5000);
    assert_eq!(rec["resume"]["handle"], SID);
    assert_eq!(rec["resume"]["head"], HEAD);
    assert_eq!(rec["resume"]["agent"], "claude");
    assert_eq!(h.lines.len(), 1, "{:?}", h.lines);
    assert!(
        h.lines[0].contains("event=stop name=writer-0644 age_s=4000 reason=done ram_freed_mb=1000"),
        "{}",
        h.lines[0]
    );
}

#[test]
fn status_without_done_or_head_does_not_stop() {
    let r = rig("writer", json!({}));
    write(
        &r,
        "lane-a.status",
        "DONE k abc\nHEAD: 0123456789abcdef0123456789abcdef01234567\n",
    );
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.status", "lane-a DONE\nHEAD: nothex\n");
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty() && h.lines.is_empty());
}

#[test]
fn a_session_still_writing_is_not_stopped_by_the_timer_path() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.activity = Some(4990);
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty());
    stop_done(&mut h, &r.cfg, 0, None); // the Stop hook path: the turn has ended
    assert_eq!(h.runs.len(), 1);
}

#[test]
fn stop_is_refused_without_a_resume_handle() {
    let r = rig("writer", json!({"session_id": null}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty());
    assert!(h.lines[0].contains("event=stop-skipped") && h.lines[0].contains("no-resume-handle"));
    assert!(record(&r, "writer").get("resume").is_none());
}

#[test]
fn a_failed_stop_command_leaves_the_record_untouched() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.fail_run = true;
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(record(&r, "writer").get("resume").is_none());
    assert!(h.lines[0].contains("event=stop-failed"));
}

#[test]
fn findings_resume_the_same_session_with_exactly_the_file_text() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    write(
        &r,
        "lane-a.fix-round-1.txt",
        "old round, delivered by hand\n",
    );
    touch(&r, "lane-a.fix-round-1.txt", 9_000);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(
        h.runs.len(),
        1,
        "a file that existed at the stop is history"
    );

    let text = "REQUEST_CHANGES\n  HIGH foo.rs:3 off by one\n\n";
    write(&r, "lane-a.fix-round-2.txt", text);
    h.now = 6000;
    resume_findings(&mut h, &r.cfg);
    let (cwd, argv) = h.runs.last().unwrap();
    assert_eq!(cwd.as_deref(), Some(r.wt.as_path()));
    assert_eq!(argv[..4], ["claude", "--bg", "--resume", SID]);
    assert_eq!(argv[4], text);
    let rec = record(&r, "writer");
    assert_eq!(rec["state"], "running");
    assert!(rec["stopped_ts"].is_null());
    assert_eq!(rec["resume"]["round"], 2);
    let line = h.lines.last().unwrap();
    assert!(
        line.contains("event=resume name=writer-0644")
            && line.contains("reason=findings")
            && line.contains("round=2")
            && line.contains("worktree=clean")
            && line.contains("stopped_for_s=1000"),
        "{line}"
    );

    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 2, "one findings file resumes once");
}

#[test]
fn the_old_done_line_does_not_stop_a_resumed_writer_but_a_new_head_does() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    resume_findings(&mut h, &r.cfg);
    let runs = h.runs.len();
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(h.runs.len(), runs, "stale DONE for the reviewed head");
    done(&r, HEAD2);
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(h.runs.len(), runs + 1);
    let rec = record(&r, "writer");
    assert_eq!(rec["resume"]["status_head"], HEAD2);
    assert_eq!(rec["resume"]["round"], 1, "the consumed round is kept");
}

#[test]
fn a_dirty_or_moved_worktree_is_resumed_but_the_log_says_so() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.dirty = Some(true);
    h.head = Some(HEAD2.into());
    resume_findings(&mut h, &r.cfg);
    assert!(
        h.lines.last().unwrap().contains("worktree=dirty+moved"),
        "{:?}",
        h.lines
    );
}

#[test]
fn low_ram_defers_the_resume_without_consuming_the_findings() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.mem = vec![100];
    h.mem_reads.set(0);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert!(
        h.lines.last().unwrap().contains("event=resume-deferred")
            && h.lines.last().unwrap().contains("low-ram")
    );
    h.mem = vec![9000];
    h.mem_reads.set(0);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 2);
}

#[test]
fn empty_and_oversized_findings_are_never_sent() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", " \n");
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    write(&r, "lane-a.fix-round-2.txt", &"x".repeat(100_001));
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert!(h.lines.last().unwrap().contains("event=resume-refused"));
    resume_findings(&mut h, &r.cfg);
    assert_eq!(
        h.lines
            .iter()
            .filter(|l| l.contains("resume-refused"))
            .count(),
        1,
        "refused once"
    );
}

#[test]
fn codex_and_opencode_resume_detached_by_their_own_handle() {
    for (agent, key, want) in [
        ("codex", "thread_id", ["codex", "exec", "resume"]),
        (
            "opencode",
            "opencode_session",
            ["opencode", "run", "--session"],
        ),
    ] {
        let r = rig(
            "writer",
            json!({"agent": agent, key: "T-1", "session_id": null, "pid": 4242, "pid_start": 9}),
        );
        done(&r, HEAD);
        let mut h = Fake::new(5000);
        h.procs.insert(4242, 9);
        stop_done(&mut h, &r.cfg, 60, None);
        assert_eq!(h.runs[0].1, ["kill", "4242"]);
        assert_eq!(record(&r, "writer")["resume"]["handle"], "T-1");
        write(&r, "lane-a.fix-round-1.txt", "fix it");
        resume_findings(&mut h, &r.cfg);
        assert_eq!(h.spawns[0][..3], want);
        assert_eq!(h.spawns[0][3..], ["T-1", "fix it"], "{agent}");
    }
}

#[test]
fn idle_for_thirty_minutes_stops_and_a_shorter_idle_does_not() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(10_000);
    h.activity = Some(10_000 - 1799);
    reap_idle(&mut h, &r.cfg);
    assert!(h.runs.is_empty());
    h.activity = Some(10_000 - 1800);
    reap_idle(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert!(
        h.lines[0].contains("event=stop name=writer-0644") && h.lines[0].contains("reason=idle"),
        "{}",
        h.lines[0]
    );
    assert_eq!(record(&r, "writer")["resume"]["handle"], SID);
}

#[test]
fn allowlisted_sessions_are_never_reaped() {
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    for name in ["build-L1", "plan-order"] {
        let r = rig(name, json!({}));
        reap_idle(&mut h, &r.cfg);
        assert!(h.runs.is_empty(), "{name} is on the allowlist");
    }
}

#[test]
fn a_findings_file_newer_than_the_last_activity_is_resumed_after_the_idle_stop() {
    let r = rig("writer", json!({}));
    write(&r, "lane-a.fix-round-1.txt", "old\n");
    touch(&r, "lane-a.fix-round-1.txt", 90_000);
    write(&r, "lane-a.fix-round-2.txt", "new findings\n");
    touch(&r, "lane-a.fix-round-2.txt", 99_500);
    let mut h = Fake::new(100_000);
    h.activity = Some(95_000);
    reap_idle(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.last().unwrap().1[4], "new findings\n");
}

#[test]
fn a_stopped_record_is_not_stopped_twice() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    reap_idle(&mut h, &r.cfg);
    reap_idle(&mut h, &r.cfg);
    stop_done(&mut h, &r.cfg, 0, None);
    assert_eq!(h.runs.len(), 1);
}

// ---- fix round 1 ----

fn j(state: &str, tasks: u64) -> Option<JobState> {
    Some(JobState {
        state: state.into(),
        tasks,
    })
}

#[test]
fn a_findings_file_between_done_and_the_stop_is_still_sent() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    write(&r, "lane-a.fix-round-1.txt", "arrived before the stop\n");
    touch(&r, "lane-a.fix-round-1.txt", 10_050);
    let mut h = Fake::new(20_000);
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(record(&r, "writer")["resume"]["round"], 0);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.last().unwrap().1[4], "arrived before the stop\n");
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
}

#[test]
fn only_the_newest_stopped_record_of_a_lane_resumes_and_a_live_one_blocks() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    add_record(
        &r,
        "writer-0700",
        json!({"session_id": "2222222a-91d1-442a-bc81-f5dd71769181", "state": "stopped", "stopped_ts": 4000,
        "resume": {"agent": "claude", "handle": "2222222a-91d1-442a-bc81-f5dd71769181", "head": HEAD, "status_head": HEAD, "round": 0, "stopped_ts": 4000}}),
    );
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.now = 6000;
    resume_findings(&mut h, &r.cfg);
    let resumes: Vec<_> = h
        .runs
        .iter()
        .filter(|(_, a)| a.get(1).map(String::as_str) == Some("--bg"))
        .collect();
    assert_eq!(resumes.len(), 1, "one file, one launch");
    assert_eq!(
        resumes[0].1[3], SID,
        "the record stopped last (5000 > 4000) wins"
    );

    // a live record of the lane blocks every stopped one
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    add_record(&r, "writer-0800", json!({}));
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
}

#[test]
fn findings_for_another_head_are_refused_once() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    done(&r, HEAD2); // another session finished this lane at another head
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    resume_findings(&mut h, &r.cfg);
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.len(), 1);
    assert_eq!(
        h.lines
            .iter()
            .filter(|l| l.contains("why=head-mismatch"))
            .count(),
        1,
        "{:?}",
        h.lines
    );
}

#[test]
fn the_round_is_claimed_before_the_launch_and_given_back_when_it_fails() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.fail_run = true;
    resume_findings(&mut h, &r.cfg);
    assert_eq!(
        record(&r, "writer")["resume"]["round"],
        0,
        "a failed launch gives the round back"
    );
    assert_eq!(record(&r, "writer")["state"], "stopped");
    assert!(h.lines.last().unwrap().contains("event=resume-failed"));
    h.fail_run = false;
    resume_findings(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
}

#[test]
fn a_working_job_with_an_old_transcript_is_not_stopped() {
    for (state, tasks) in [("working", 0), ("blocked", 0), ("done", 2)] {
        let r = rig("writer", json!({}));
        let mut h = Fake::new(100_000);
        h.activity = Some(1);
        h.job = j(state, tasks);
        reap_idle(&mut h, &r.cfg);
        done(&r, HEAD);
        stop_done(&mut h, &r.cfg, 0, None);
        assert!(h.runs.is_empty(), "{state}/{tasks}");
        assert!(h.lines.is_empty() && record(&r, "writer").get("resume").is_none());
    }
}

#[test]
fn a_job_the_runtime_already_stopped_only_gets_its_resume_handle_recorded() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.job = j("stopped", 0);
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty());
    assert_eq!(record(&r, "writer")["resume"]["handle"], SID);
    assert!(h.lines[0].contains("event=stop name=writer-0644"));
}

#[test]
fn an_unreadable_job_state_keeps_the_session_and_logs_once() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.job = None;
    stop_done(&mut h, &r.cfg, 60, None);
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty());
    assert_eq!(h.lines.len(), 1, "{:?}", h.lines);
    assert!(h.lines[0].contains("why=no-job-state"));
}

#[test]
fn a_reused_pid_is_never_signalled_and_a_resume_stores_the_new_identity() {
    let r = rig(
        "writer",
        json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242, "pid_start": 9}),
    );
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    h.procs.insert(4242, 10); // another process now owns the pid
    stop_done(&mut h, &r.cfg, 60, None);
    assert!(h.runs.is_empty(), "no kill of a stranger");
    assert_eq!(record(&r, "writer")["resume"]["handle"], "T-1");

    write(&r, "lane-a.fix-round-1.txt", "fix it");
    resume_findings(&mut h, &r.cfg);
    let rec = record(&r, "writer");
    assert_eq!(
        (rec["pid"].as_u64(), rec["pid_start"].as_u64()),
        (Some(777), Some(55))
    );

    // the next stop checks the new identity
    done(&r, HEAD2);
    stop_done(&mut h, &r.cfg, 60, None);
    assert_eq!(h.runs.last().unwrap().1, ["kill", "777"]);
}

#[test]
fn a_non_claude_record_without_process_identity_is_not_stopped() {
    for extra in [
        json!({"agent": "codex", "thread_id": "T-1", "session_id": null, "pid": 4242}),
        json!({"agent": "opencode", "opencode_session": "S-1", "session_id": null}),
    ] {
        let r = rig("writer", extra);
        done(&r, HEAD);
        let mut h = Fake::new(5000);
        stop_done(&mut h, &r.cfg, 60, None);
        assert!(h.runs.is_empty());
        assert!(h.lines[0].contains("why=no-process-identity"));
    }
}

#[test]
fn an_idle_stop_without_a_status_head_keeps_the_git_head_for_the_moved_check() {
    let r = rig("writer", json!({}));
    let mut h = Fake::new(100_000);
    h.activity = Some(1);
    reap_idle(&mut h, &r.cfg);
    assert_eq!(record(&r, "writer")["resume"]["head"], HEAD);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.head = Some(HEAD2.into());
    resume_findings(&mut h, &r.cfg);
    assert!(
        h.lines.last().unwrap().contains("worktree=clean+moved"),
        "{:?}",
        h.lines
    );
}

#[test]
fn a_failed_dirty_check_is_logged_as_unknown_never_clean() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    h.dirty = None;
    resume_findings(&mut h, &r.cfg);
    assert!(
        h.lines.last().unwrap().contains("worktree=unknown"),
        "{:?}",
        h.lines
    );
}

#[test]
fn the_lane_gets_a_resume_file_at_the_stop_and_at_the_resume() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    let lane: Value =
        serde_json::from_slice(&std::fs::read(r.cfg.pr_dir.join("lane-a.resume.json")).unwrap())
            .unwrap();
    assert_eq!(
        (
            lane["state"].as_str(),
            lane["handle"].as_str(),
            lane["head"].as_str()
        ),
        (Some("stopped"), Some(SID), Some(HEAD))
    );
    write(&r, "lane-a.fix-round-1.txt", "fix it");
    resume_findings(&mut h, &r.cfg);
    let lane: Value =
        serde_json::from_slice(&std::fs::read(r.cfg.pr_dir.join("lane-a.resume.json")).unwrap())
            .unwrap();
    assert_eq!(
        (lane["state"].as_str(), lane["round"].as_u64()),
        (Some("running"), Some(1))
    );
}
