use pg_fleet::digest::Runner;
use pg_fleet::process::Output;
use serde_json::Value;
use std::path::Path;
struct Recorded(Vec<Value>);
impl Runner for Recorded {
    fn run(&self, p: &str, a: &[String], _: Option<&Path>) -> Result<Output, String> {
        let a: Vec<_> = a.iter().map(|s| s.replace('\\', "/")).collect();
        let v = self
            .0
            .iter()
            .find(|v| v["name"] == p && v["args"] == serde_json::json!(a))
            .expect("unexpected command");
        Ok(Output {
            code: v["code"].as_i64().unwrap() as i32,
            stdout: v["stdout"].as_str().unwrap().into(),
            stderr: String::new(),
        })
    }
}
#[test]
fn original_work_summary_executions() {
    for raw in include_str!("fixtures/summary.jsonl").lines() {
        let v: Value = serde_json::from_str(raw).unwrap();
        let d = tempfile::tempdir().unwrap();
        let base = d.path().to_string_lossy().replace('\\', "/");
        let len = v["root_len"].as_u64().unwrap() as usize;
        let root = if len > base.len() + 1 {
            d.path().join("x".repeat(len - base.len() - 1))
        } else {
            d.path().to_owned()
        };
        let rootstr = root.to_string_lossy().replace('\\', "/");
        let v: Value = serde_json::from_str(&raw.replace("@ROOT@", &rootstr)).unwrap();
        for dir in v["dirs"].as_array().unwrap() {
            std::fs::create_dir_all(root.join(dir.as_str().unwrap())).unwrap();
        }
        for f in v["files"].as_array().unwrap() {
            let p = root.join(f[0].as_str().unwrap());
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(p, f[1].as_str().unwrap()).unwrap();
        }
        let mut c = pg_fleet::summary::Config::isolated(&root);
        c.now = pg_fleet::digest_text::to_dt(v["now"].as_str().unwrap()).unwrap();
        c.keeper = v["keeper"]
            .as_str()
            .filter(|s| !s.is_empty())
            .map(Into::into);
        for args in v["args"].as_array().unwrap().as_chunks::<2>().0 {
            match args[0].as_str().unwrap() {
                "--since" => c.since = args[1].as_str().unwrap().into(),
                "--project" => c.project = args[1].as_str().map(str::to_owned),
                _ => panic!("unknown flag"),
            }
        }
        let before: Vec<_> = v["files"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| {
                let p = root.join(f[0].as_str().unwrap());
                (
                    p.clone(),
                    std::fs::read(&p).unwrap(),
                    p.metadata().unwrap().modified().unwrap(),
                )
            })
            .collect();
        let got =
            pg_fleet::summary::build(&c, &Recorded(v["commands"].as_array().unwrap().clone()))
                .unwrap();
        assert_eq!(
            serde_json::json!(got.iter().map(|s| s.replace('\\', "/")).collect::<Vec<_>>()),
            v["expected"],
            "{}",
            v["name"]
        );
        for (p, bytes, mtime) in before {
            assert_eq!(std::fs::read(&p).unwrap(), bytes);
            assert_eq!(p.metadata().unwrap().modified().unwrap(), mtime);
        }
    }
}
