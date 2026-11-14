//! Recorded executions of the original test_resume_digest.py scenarios.
//! Files and subprocess replies are fixtures; no Python, network or real home.
use pg_fleet::digest::{Config, Runner};
use pg_fleet::process::Output;
use serde_json::Value;
use std::path::{Path, PathBuf};

struct Recorded(Vec<Value>);
impl Runner for Recorded {
    fn run(&self, name: &str, args: &[String], _cwd: Option<&Path>) -> Result<Output, String> {
        let args: Vec<_> = args
            .iter()
            .map(|s| s.replace('\\', "/").trim_start_matches("//?/").to_owned())
            .collect();
        let found = self
            .0
            .iter()
            .find(|v| v["name"] == name && v["args"] == serde_json::json!(args));
        let Some(v) = found else {
            return Err("FileNotFoundError".into());
        };
        if let Some(code) = v["code"].as_i64() {
            Ok(Output {
                code: code as i32,
                stdout: v["stdout"].as_str().unwrap().into(),
                stderr: String::new(),
            })
        } else {
            Err(v["stdout"].as_str().unwrap().into())
        }
    }
}
#[test]
fn original_digest_scenarios() {
    let mut failures = vec![];
    let mut count = 0;
    for raw in include_str!("fixtures/digest.jsonl").lines() {
        count += 1;
        let original: Value = serde_json::from_str(raw).unwrap();
        let d = tempfile::tempdir().unwrap();
        let base = d.path().to_string_lossy().replace('\\', "/");
        let target = original["root_len"].as_u64().unwrap() as usize;
        let root = if target > base.len() + 1 {
            d.path().join("x".repeat(target - base.len() - 1))
        } else {
            d.path().to_owned()
        };
        std::fs::create_dir_all(&root).unwrap();
        let rootstr = root.to_string_lossy().replace('\\', "/");
        let slug = pg_fleet::handoff::re("[^a-zA-Z0-9]").replace_all(&rootstr, "-");
        let v: Value =
            serde_json::from_str(&raw.replace("@ROOT@", &rootstr).replace("@SLUG@", &slug))
                .unwrap();
        for p in v["dirs"].as_array().unwrap() {
            std::fs::create_dir_all(root.join(p.as_str().unwrap())).unwrap();
        }
        for f in v["files"].as_array().unwrap() {
            let p = root.join(f[0].as_str().unwrap());
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(&p, f[1].as_str().unwrap()).unwrap();
            let t =
                std::time::UNIX_EPOCH + std::time::Duration::from_secs_f64(f[2].as_f64().unwrap());
            std::fs::File::options()
                .write(true)
                .open(p)
                .unwrap()
                .set_times(std::fs::FileTimes::new().set_modified(t))
                .unwrap();
        }
        #[cfg(unix)]
        for l in v["links"].as_array().unwrap() {
            std::os::unix::fs::symlink(l[1].as_str().unwrap(), root.join(l[0].as_str().unwrap()))
                .unwrap();
        }
        let env = &v["env"];
        let path = |k: &str| PathBuf::from(env[k].as_str().unwrap());
        let mut c = Config::isolated(&root);
        c.home = root.join("home");
        c.routing = root.join("rstate");
        c.fleet = root.join("fleet");
        c.data = path("RESUME_DIGEST_HOME");
        c.snapshot = path("RESUME_DIGEST_SNAPSHOT");
        c.spawner = path("RESUME_DIGEST_SPAWNER_DIR");
        c.dlog = path("RESUME_DIGEST_DLOG");
        c.cwd = v["cwd"].as_str().unwrap().into();
        c.sid = v["sid"].as_str().unwrap().into();
        c.handoff_cap = env["AUTOOS_CTX_HANDOFF"].as_str().map(str::to_owned);
        c.hard_cap = env["AUTOOS_CTX_HARD"].as_str().map(str::to_owned);
        let runner = Recorded(v["commands"].as_array().unwrap().clone());
        let arg = v["args"].as_array().unwrap().get(1).and_then(Value::as_str);
        let actual = pg_fleet::digest::build(c, arg, &runner);
        let actual: Vec<_> = actual.into_iter().map(|s| s.replace('\\', "/")).collect();
        let expected: Vec<_> = v["expected"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_owned())
            .collect();
        if actual != expected {
            let diff = actual
                .iter()
                .zip(&expected)
                .position(|(a, b)| a != b)
                .unwrap_or(actual.len().min(expected.len()));
            failures.push(format!(
                "{} line {diff}:\n actual: {:?}\n expect: {:?}",
                v["name"],
                actual.get(diff),
                expected.get(diff)
            ));
        }
    }
    assert!(count >= 100, "expected every executable scenario");
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
