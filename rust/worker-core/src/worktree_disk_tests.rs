use super::*;

#[cfg(target_os = "linux")]
#[path = "worktree_disk_reap_tests.rs"]
mod teardown;

#[path = "worktree_disk_cap_tests.rs"]
mod cap;

#[test]
fn legacy_target_is_removed_but_shared_cache_is_kept() {
    let root = tempfile::tempdir().unwrap();
    let tree = root.path().join("tree");
    std::fs::create_dir_all(tree.join("rust/target/debug")).unwrap();
    std::fs::write(tree.join("rust/target/debug/artifact"), b"old build").unwrap();
    std::fs::write(tree.join("rust/Cargo.toml"), b"source").unwrap();
    remove_legacy_target(&tree).unwrap();
    assert!(!tree.join("rust/target").exists());
    assert!(tree.join("rust/Cargo.toml").exists());
    remove_legacy_target(&tree).unwrap();
}

#[cfg(unix)]
#[test]
fn legacy_target_link_does_not_delete_shared_cache() {
    let root = tempfile::tempdir().unwrap();
    let tree = root.path().join("tree");
    let shared = root.path().join("shared");
    std::fs::create_dir_all(tree.join("rust")).unwrap();
    std::fs::create_dir(&shared).unwrap();
    std::fs::write(shared.join("artifact"), b"shared build").unwrap();
    std::os::unix::fs::symlink(&shared, tree.join("rust/target")).unwrap();
    remove_legacy_target(&tree).unwrap();
    assert!(shared.join("artifact").exists());
    assert!(!tree.join("rust/target").exists());
}

#[cfg(unix)]
#[test]
fn rust_directory_link_does_not_delete_external_target() {
    let root = tempfile::tempdir().unwrap();
    let tree = root.path().join("tree");
    let external = root.path().join("external");
    std::fs::create_dir(&tree).unwrap();
    std::fs::create_dir_all(external.join("target")).unwrap();
    std::fs::write(external.join("target/artifact"), b"keep").unwrap();
    std::os::unix::fs::symlink(&external, tree.join("rust")).unwrap();
    remove_legacy_target(&tree).unwrap();
    assert!(external.join("target/artifact").exists());
}

// Real concurrent writer processes, driven through the production Python guard.
// The subprocess bridge is necessary until the spawner's Python file is ported.
#[test]
fn parallel_writers_are_stopped_at_emergency_floor_and_idle_cache_is_reclaimed() {
    let root = tempfile::tempdir().unwrap();
    let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new("python3")
        .args([
            "-c",
            r#"
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from plangraph.spawner import host_ready as h
from plangraph.spawner.runner import SpawnerRunner, JobInfo
from plangraph.spawner.proc_kill import alive, escapees, root_identity
from plangraph.spawner.winjob import WinJobApi

async def main():
    cwd = Path(sys.argv[1]) / 'checkout'
    cwd.mkdir()
    target = h.shared_cargo_target()
    target.mkdir()
    runner = SpawnerRunner(default_cwd=str(cwd))
    runner._cargo_leases = {}
    runner._win_jobs = {}
    if os.name == 'nt':
        runner.win_job_api = WinJobApi()
    tasks = []
    processes = []
    for index in range(3):
        key = str(index)
        env = h.cargo_child_env(dict(os.environ, DISK_TEST_WRITER=key))
        proc = await asyncio.create_subprocess_exec(sys.argv[2], '--ignored', '--exact',
            'worktree::disk::tests::disk_test_writer_process', env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        processes.append(proc)
        job = JobInfo(key, 'running', 'write', 'test', cwd=str(cwd))
        runner.jobs[key] = job
        runner._child_envs[key] = env
        runner._processes[key] = proc
        runner._owned = {**getattr(runner, '_owned', {}), key:
            dict(pid=proc.pid, pid_start=root_identity(proc.pid), job_object=False)}
        if os.name == 'nt':
            # Match production launch: Windows verification needs a Job Object
            # after taskkill has removed the root from the process table.
            handle = runner.win_job_api.create_job()
            runner._win_jobs[key] = handle
            runner.win_job_api.assign(handle, proc.pid)
            runner._owned[key]['job_object'] = handle
            runner._escapees = {**getattr(runner, '_escapees', {}), key:
                escapees(proc.pid, runner.win_job_api.pids(handle))}
        runner._cargo_leases[key] = h.cargo_lease()
        tasks.append(asyncio.create_task(h.collect_with_disk_guard(runner, key, proc)))
    try:
        real_disk_free = h.disk_free_gb
        h.disk_free_gb = lambda path: 1.99
        await asyncio.wait_for(asyncio.gather(*tasks), 15)
        h.disk_free_gb = real_disk_free
        await asyncio.gather(*(proc.wait() for proc in processes))
        assert not runner._cargo_leases, runner._cargo_leases
        assert not any(j.survivors for j in runner.jobs.values())
        assert not any(alive(proc.pid, None) for proc in processes)
        # Even idle service records cannot authorize eviction under a foreign lease.
        with h.cargo_lease():
            busy = h.local_disk_refusal(cwd, SimpleNamespace(ps=lambda: {'runs': []}))
        size = h.cargo_target_size_gb(target)
        idle = h.local_disk_refusal(cwd, SimpleNamespace(ps=lambda: {'runs': []}))
        print(json.dumps(dict(statuses=[j.status for j in runner.jobs.values()],
            reasons=[j.refusal for j in runner.jobs.values()], busy=busy, idle=idle,
            size=size, reclaimed=not target.exists())))
    finally:
        for handle in runner._win_jobs.values():
            runner.win_job_api.close(handle)
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
        await asyncio.gather(*(proc.wait() for proc in processes))
        for lease in runner._cargo_leases.values():
            lease.close()
asyncio.run(main())
"#,
        ])
        .arg(root.path())
        .arg(std::env::current_exe().unwrap())
        .env("PYTHONPATH", repo.join("src"))
        .env("HOME", root.path().join("home"))
        .env_remove("PLANGRAPH_SETTINGS")
        .env("PLANGRAPH_CARGO_TARGET_DIR", root.path().join("target"))
        .env("PLANGRAPH_CARGO_TARGET_CAP_GB", "0.0078125") // 8 MiB
        // Cache-cap admission is independent of the temporary filesystem's free space.
        .env("PLANGRAPH_DISK_FLOOR_GB", "0")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        result["statuses"],
        serde_json::json!(["failed", "failed", "failed"])
    );
    for reason in result["reasons"].as_array().unwrap() {
        assert!(reason.as_str().unwrap().starts_with("disk_low:"));
    }
    assert!(result["busy"]
        .as_str()
        .unwrap()
        .starts_with("cargo_target_cap:"));
    assert!(result["idle"].is_null(), "{result}");
    assert_eq!(result["reclaimed"], true);
    assert_eq!(result["size"].as_f64().unwrap(), 12.0 / 1024.0);
}

#[test]
#[ignore = "subprocess helper for parallel cache-cap test"]
fn disk_test_writer_process() {
    use std::{env, fs, thread, time::Duration};
    let Ok(index) = env::var("DISK_TEST_WRITER") else {
        return;
    };
    assert_eq!(env::var("CARGO_INCREMENTAL").unwrap(), "0");
    let target = std::path::PathBuf::from(env::var("CARGO_TARGET_DIR").unwrap());
    fs::write(target.join(index), vec![0_u8; 4 * 1024 * 1024]).unwrap();
    loop {
        thread::sleep(Duration::from_secs(1));
    }
}

#[test]
fn runtime_disk_stop_uses_absolute_two_gb_and_fails_closed() {
    let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new("python3")
        .args([
            "-c",
            r#"
import json
from plangraph.spawner import host_ready as h
h.disk_free_gb = lambda path: 1.99
low = h.running_disk_refusal('.')
h.disk_free_gb = lambda path: 2
h.cargo_target_size_gb = lambda path: 0
boundary = h.running_disk_refusal('.')
def unreadable(path):
    raise PermissionError()
h.disk_free_gb = unreadable
error = h.running_disk_refusal('.')
print(json.dumps([low, boundary, error]))
"#,
        ])
        .env("PYTHONPATH", repo.join("src"))
        .env_remove("PLANGRAPH_SETTINGS")
        .env("PLANGRAPH_CARGO_TARGET_CAP_GB", "12")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert!(result[0].as_str().unwrap().contains("stop 2 GB"));
    assert!(result[1].is_null());
    assert!(result[2].as_str().unwrap().contains("unavailable"));
}

fn disk_bridge(script: &str) -> serde_json::Value {
    let root = tempfile::tempdir().unwrap();
    let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new("python3")
        .args(["-c", script])
        .arg(root.path())
        .env("PYTHONPATH", repo.join("src"))
        .env("HOME", root.path().join("home"))
        .env_remove("PLANGRAPH_SETTINGS")
        .env("PLANGRAPH_CARGO_TARGET_DIR", root.path().join("target"))
        .env("PLANGRAPH_CARGO_TARGET_CAP_GB", "12")
        .env("XDG_CONFIG_HOME", root.path().join("config"))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn disk_guard_waits_for_verification_and_lease_release_even_when_cancelled() {
    let result = disk_bridge(
        r#"
import asyncio, json
from types import SimpleNamespace
from plangraph.spawner import host_ready as h, runner as r, run_watchdog

async def case(cancel):
    started = asyncio.Event()
    verified = []
    async def collect(*args):
        await started.wait()
        if cancel:
            await asyncio.sleep(10)
        return b'output', b''
    async def reap(*args):
        started.set()  # pipes finish before descendant verification does
        await asyncio.sleep(.1)
        verified.append(True)
    run_watchdog.collect = collect
    r.reap = reap
    h.running_disk_refusal = lambda cwd: 'disk_low: test'
    runner = r.SpawnerRunner()
    job = r.JobInfo('j', 'running', 'task', 'test', cwd='.')
    runner.jobs['j'] = job
    lease = h.cargo_lease()
    runner._cargo_leases = {'j': lease}
    task = asyncio.create_task(h.collect_with_disk_guard(runner, 'j', SimpleNamespace()))
    if cancel:
        await started.wait()
        task.cancel()
    cancelled = False
    try:
        await asyncio.wait_for(task, 5)
    except asyncio.CancelledError:
        cancelled = True
    return dict(verified=verified, closed=lease.closed,
                leases=list(runner._cargo_leases), cancelled=cancelled)
async def main():
    print(json.dumps([await case(False), await case(True)]))
asyncio.run(main())
"#,
    );
    for (index, case) in result.as_array().unwrap().iter().enumerate() {
        assert_eq!(case["verified"], serde_json::json!([true]));
        assert_eq!(case["closed"], true);
        assert_eq!(case["leases"], serde_json::json!([]));
        assert_eq!(case["cancelled"], index == 1);
    }
}

#[test]
fn disk_stop_is_terminal_for_both_dispatchers_without_booking_or_retries() {
    let result = disk_bridge(
        r#"
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner import exit_outcome as outcome, resolver, leg_probe, base_fresh, service as service_mod, runner as runner_mod
from plangraph.spawner.runner import JobInfo, spawn_service
from plangraph.spawner.service import SpawnService

def forbidden(*args, **kwargs):
    raise AssertionError('disk stop booked or continued a healthy provider')
async def no_continuation(*args, **kwargs):
    forbidden()
async def admit(plan, **kwargs):
    return plan
async def no_probe(*args, **kwargs):
    return None
async def fresh(*args, **kwargs):
    return True
base_fresh.admit_base = service_mod.admit_base = runner_mod.admit_base = fresh
outcome._book_failure = forbidden
resolver.plan_for_launch_async = admit
leg_probe.before_launch = no_probe
chosen = next(leg for leg in DEFAULT_LEGS if leg.id == 'claude-haiku-5.5')
plan = SpawnPlan((chosen, chosen), (), 'test', (), ())

async def case(hardened, role, reason):
    class Runner:
        default_cwd = str(Path.cwd())
        _processes, _tasks, provider_responses = {}, {}, {}
        calls = 0
        async def spawn(self, **kwargs):
            self.calls += 1
            job = JobInfo(str(self.calls), 'failed', 'task', chosen.model, role=role,
                          exit_code=7, stdout='usage limit reached', cwd=self.default_cwd)
            job.refusal = reason
            job.events = [dict(event='disk_run_stopped', reason=reason),
                          dict(event='opencode_reasoning_exhausted', cap_available=True)]
            return job
    runner = Runner()
    state = Path(sys.argv[1]) / f'{hardened}-{role}-{reason.split(":")[0]}'
    service = spawn_service(runner, state) if hardened else SpawnService(runner, state)
    service._continue_writer = no_continuation
    record = await service._spawn(card={'estimate_tokens': 100}, prompt='task',
                                  role=role, writer_families=['openai'] if role == 'review' else [],
                                  _plan=plan, wait_seconds=None)
    job = JobInfo('direct', 'failed', 'task', chosen.model, role=role, exit_code=7)
    job.refusal = reason
    job.events = [dict(event='disk_run_stopped', reason=reason)]
    direct = {}
    fallthrough = outcome.fallthrough_reason(SimpleNamespace(), chosen, job, direct)
    return dict(calls=runner.calls, status=record['status'], reason=record['reason'],
                detail=record['disk_refusal'], attempts=len(record['attempts']),
                fallthrough=fallthrough, direct_reason=direct['reason'])
async def main():
    print(json.dumps([await case(hardened, role, reason)
        for hardened in (False, True) for role in ('review', 'implement')
        for reason in ('cargo_target_cap: test', 'disk_low: test')]))
asyncio.run(main())
"#,
    );
    assert_eq!(result.as_array().unwrap().len(), 8);
    for case in result.as_array().unwrap() {
        assert_eq!(case["calls"], 1);
        assert_eq!(case["attempts"], 1);
        assert_eq!(case["status"], "failed");
        assert_eq!(case["reason"], "disk_run_stopped");
        assert_eq!(case["direct_reason"], "disk_run_stopped");
        assert!(case["fallthrough"].is_null());
        assert!(case["detail"].as_str().unwrap().ends_with(": test"));
    }
}

#[test]
fn admission_refuses_busy_cache_at_exact_cap_and_zero_capacity() {
    let result = disk_bridge(
        r#"
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from plangraph.spawner import host_ready as h
h.cargo_target_size_gb = lambda target: 12
service = SimpleNamespace(ps=lambda: {'runs': [{'status': 'running'}]})
exact = h.local_disk_refusal(Path(sys.argv[1]) / 'checkout', service)
os.environ['PLANGRAPH_CARGO_TARGET_CAP_GB'] = '0'
h.cargo_target_size_gb = lambda target: 0
zero = h.local_disk_refusal(Path(sys.argv[1]) / 'checkout', SimpleNamespace(ps=lambda: {'runs': []}))
print(json.dumps([exact, zero]))
"#,
    );
    for reason in result.as_array().unwrap() {
        assert!(reason.as_str().unwrap().starts_with("cargo_target_cap:"));
    }
}
