#![cfg(unix)]

use std::{
    fs,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
    process::{self, Command},
    time::{SystemTime, UNIX_EPOCH},
};

struct Fixture(PathBuf);

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn hourly_router_heartbeat_stays_quiet_and_a_missed_beat_alerts_once() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();
    let fixture = Fixture(std::env::temp_dir().join(format!(
        "router-liveness-{}-{}",
        process::id(),
        now.as_nanos()
    )));
    fs::create_dir(&fixture.0).unwrap();
    let config = root.join("deploy/liveness/router.toml");
    assert!(config.is_file(), "install must provide router.toml");

    let stamp = fixture
        .0
        .join(".local/state/router-liveness/routing-00.json");
    fs::create_dir_all(stamp.parent().unwrap()).unwrap();
    let sender = fixture.0.join("code/plangraph/tools/fleet-msg");
    fs::create_dir_all(sender.parent().unwrap()).unwrap();
    symlink("/bin/true", sender).unwrap();

    let tick = |age_min: u64| {
        fs::write(
            &stamp,
            format!("{{\"ts\":{}}}", now.as_secs() - age_min * 60),
        )
        .unwrap();
        let output = Command::new("python3")
            .arg(root.join("tools/liveness_watchdog.py"))
            .arg("--config")
            .arg(&config)
            .arg("--apply")
            .current_dir(&fixture.0)
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        String::from_utf8(output.stdout).unwrap()
    };
    for _hour in 0..6 {
        for age in [0, 10, 20, 30, 40, 50, 55, 60] {
            assert!(tick(age).is_empty(), "healthy heartbeat at age {age}");
        }
    }
    assert!(tick(70).contains("routing-00: alert"));
    for age in [80, 100, 120, 130] {
        assert!(
            !tick(age).contains("routing-00: alert ("),
            "repeated alert at age {age}"
        );
    }
    assert!(tick(0).contains("episode reset"));
    assert!(tick(70).contains("routing-00: alert"));
}
