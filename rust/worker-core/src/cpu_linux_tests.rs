use super::*;
use crate::cpu::Sampler;
use std::{
    os::unix::process::CommandExt,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

fn member(root: &std::path::Path, pid: u32, group: u32, start: u64, ticks: u64) {
    let dir = root.join(pid.to_string());
    fs::create_dir_all(&dir).unwrap();
    let mut fields = vec!["0".to_owned(); 20];
    fields[0] = "S".into();
    fields[2] = group.to_string();
    fields[11] = ticks.to_string();
    fields[13] = "999999".into(); // cutime must not count.
    fields[19] = start.to_string();
    fs::write(
        dir.join("stat"),
        format!("{pid} (fake ) sh) {}", fields.join(" ")),
    )
    .unwrap();
}
#[test]
fn proc_comm_parentheses_ticks_group_filter() {
    let dir = tempfile::tempdir().unwrap();
    member(dir.path(), 10, 7, 1, 10);
    member(dir.path(), 11, 8, 1, 999);
    let mut source = LinuxSource::new(dir.path().into(), None, 7).unwrap();
    assert_eq!(source.read().unwrap().total_ms, 0);
    member(dir.path(), 10, 7, 1, 30);
    member(dir.path(), 11, 8, 1, 9999);
    assert_eq!(source.read().unwrap().total_ms, 20 * 1000 / source.ticks);
    fs::write(dir.path().join("10/stat"), "malformed").unwrap();
    assert_eq!(source.read().unwrap().total_ms, 20 * 1000 / source.ticks);
}
#[test]
fn pid_reuse_and_member_exit_no_false_delta() {
    let dir = tempfile::tempdir().unwrap();
    member(dir.path(), 10, 7, 1, 100);
    member(dir.path(), 11, 7, 1, 200);
    let mut source = LinuxSource::new(dir.path().into(), None, 7).unwrap();
    source.read().unwrap();
    fs::remove_file(dir.path().join("11/stat")).unwrap();
    member(dir.path(), 10, 7, 2, 900);
    assert_eq!(source.read().unwrap().total_ms, 0);
    member(dir.path(), 10, 7, 2, 901);
    assert_eq!(source.read().unwrap().total_ms, 1000 / source.ticks);
}
#[test]
fn cgroup_preferred_and_fallback_baseline_reset() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cpu.stat");
    fs::write(&path, "usage_usec 1000000\nuser_usec 900000\n").unwrap();
    member(dir.path(), 10, 7, 1, 999);
    let mut source = LinuxSource::new(dir.path().into(), Some(path.clone()), 7).unwrap();
    let mut sampler = Sampler::new(1);
    assert!(sampler.poll(0, &mut || source.read()).is_none());
    fs::write(&path, "usage_usec broken").unwrap();
    assert!(sampler.poll(30_000, &mut || source.read()).is_none());
    member(dir.path(), 10, 7, 1, 1999);
    assert!(sampler.poll(60_000, &mut || source.read()).is_none());
    member(dir.path(), 10, 7, 1, 2999);
    assert!(sampler.poll(90_000, &mut || source.read()).is_some());
    fs::write(&path, "usage_usec 999999999").unwrap();
    assert!(sampler.poll(120_000, &mut || source.read()).is_none());
    assert!(sampler.poll(150_000, &mut || source.read()).is_none());
    assert!(usage("usage_usec 1\nusage_usec 2").is_none());
}
#[test]
fn inaccessible_source() {
    let dir = tempfile::tempdir().unwrap();
    assert!(LinuxSource::new(dir.path().into(), None, 0).is_err());
    let mut source = LinuxSource::new(dir.path().join("absent"), None, 7).unwrap();
    assert!(source.read().is_err());
    let mut source = LinuxSource::new(dir.path().into(), None, 7).unwrap();
    assert_eq!(source.read().unwrap().total_ms, 0);
}

struct Owned(Child);
impl Owned {
    fn spawn(script: &str) -> Self {
        Self(
            Command::new("sh")
                .args(["-c", script])
                .process_group(0)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        )
    }
    fn source(&self) -> LinuxSource {
        LinuxSource::new(PathBuf::from("/proc"), None, self.0.id()).unwrap()
    }
}
impl Drop for Owned {
    fn drop(&mut self) {
        // Only these directly owned children are killed; no foreign group signals.
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
#[test]
fn actual_busy_and_idle_fake_sh_children() {
    let busy = Owned::spawn("while :; do :; done");
    let idle = Owned::spawn("exec sleep 10");
    let mut busy_source = busy.source();
    let mut idle_source = idle.source();
    let mut busy_sampler = Sampler::new(10);
    let mut idle_sampler = Sampler::new(100);
    busy_sampler.poll(0, &mut || busy_source.read());
    idle_sampler.poll(0, &mut || idle_source.read());
    let start = Instant::now();
    let mut detected = false;
    for n in 1..=100 {
        std::thread::sleep(Duration::from_millis(20));
        assert!(idle_sampler
            .poll(n * 30_000, &mut || idle_source.read())
            .is_none());
        if busy_sampler
            .poll(n * 30_000, &mut || busy_source.read())
            .is_some()
        {
            detected = true;
            break;
        }
        if start.elapsed() > Duration::from_secs(4) {
            break;
        }
    }
    assert!(detected, "owned busy child produced no accounting delta");
}

#[test]
fn sampler_as_rl12_cpu_hook() {
    use crate::{activity::*, events::*, hosts::Role, watch::Hooks};
    let mut sampler = Sampler::new(1);
    let mut total = 0;
    let mut cpu = |now| {
        sampler.poll(now, &mut || {
            total += 1;
            Ok(CpuSample { total_ms: total })
        })
    };
    let mut monitor = Monitor::new(
        Role::Implement,
        &Limits::default(),
        Modes {
            first: Mode::Off,
            silent: Mode::Off,
            milestone: Mode::Off,
        },
        0,
    );
    let mut parser = crate::events::claude::ClaudeParser::new(
        crate::events::claude::Flavor::Claude,
        0,
        None,
        crate::events::claude::RECORDED_CLAUDE[0],
    );
    let mut channel = ChannelHealth::new(0, None);
    let mut hooks = Hooks {
        parser: &mut parser,
        monitor: &mut monitor,
        cancelled: &|| false,
        tree_sample: &Ctx::default,
        cpu: Some(&mut cpu),
        stdin: &[],
        channel: &mut channel,
    };
    assert!(hooks.cpu.as_mut().unwrap()(0).is_none());
    assert!(hooks.cpu.as_mut().unwrap()(29_999).is_none());
    assert_eq!(
        hooks.cpu.as_mut().unwrap()(30_000).unwrap().kind,
        Kind::Weak
    );
}
