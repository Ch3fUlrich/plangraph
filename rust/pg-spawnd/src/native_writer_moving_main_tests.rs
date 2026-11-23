use super::*;

struct MovingMain {
    host: Host,
    upstream: PathBuf,
    conflict: bool,
    race: bool,
    original_head: String,
}

impl MovingMain {
    fn advance_main(&self) {
        let file = if self.conflict {
            "writer.txt"
        } else {
            "train.txt"
        };
        fs::write(self.upstream.join(file), "from main\n").unwrap();
        git_in(&self.upstream, &["add", file]);
        git_in(&self.upstream, &["commit", "-qm", "train"]);
        git_in(&self.upstream, &["push", "-q", "origin", "main"]);
    }
}

impl Driver for MovingMain {
    fn prepare(&mut self) -> Result<String, String> {
        self.host.prepare()
    }
    fn refresh_base(&mut self) -> Result<String, String> {
        panic!("unexpected writer relaunch");
    }
    fn prepare_review(&mut self, head: &str) -> Result<Option<native_writer::ReviewBase>, String> {
        self.host.prepare_review(head)
    }
    fn disk_gb(&mut self) -> Result<u64, String> {
        Ok(20)
    }
    fn execute(&mut self, session: Option<&str>, _: &str) -> Result<String, String> {
        assert!(session.is_none(), "the worker owns the rebase");
        fs::write(self.host.cwd.join("writer.txt"), "from writer\n").unwrap();
        git_in(&self.host.cwd, &["add", "writer.txt"]);
        git_in(&self.host.cwd, &["commit", "-qm", "writer"]);
        self.original_head = self.host.head()?;
        if !self.race {
            self.advance_main();
        }
        Ok("fixture".into())
    }
    fn head(&mut self) -> Result<String, String> {
        self.host.head()
    }
    fn fast_checks(&mut self, head: &str) -> Result<(), String> {
        self.host.fast_checks(head)?;
        if self.race && head == self.original_head {
            self.advance_main();
        }
        Ok(())
    }
    fn stamped(&mut self, head: &str) -> bool {
        self.host.stamped(head)
    }
    fn review(&mut self, head: &str, previous: Option<&Value>) -> Result<Value, String> {
        self.host.review(head, previous)
    }
    fn gates(&mut self) -> Result<(), String> {
        self.host.gates()
    }
    fn push(&mut self, head: &str) -> Result<Option<String>, String> {
        self.host.push(head)
    }
    fn save(&mut self, record: &Value) -> Result<(), String> {
        self.host.save(record)
    }
}

#[test]
fn native_writer_reviews_and_publishes_when_main_moves() {
    for (conflict, race, lane_base) in [
        (false, false, false),
        (true, false, false),
        (false, true, false),
        (false, false, true),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let mut host = local_base_fixture(dir.path());
        host.run_id = "run-movingmain".into();
        if lane_base {
            git_in(
                &host.repo,
                &["push", "-q", "origin", "HEAD:refs/heads/phase/local-result"],
            );
        } else {
            host.base_ref = "refs/heads/main".into();
        }
        host.record_path = dir
            .path()
            .join("runs")
            .join(&host.run_id)
            .join("record.json");
        host.review_override = Some(("fixture".into(), "fixture review".into()));
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        host.python_src = Some(root.clone());
        host.review_cli = vec![
            crate::test_python::command()
                .get_program()
                .to_string_lossy()
                .into_owned(),
            "-c".into(),
            r#"
import asyncio, json, pathlib, sys, tempfile
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from plangraph.spawner.base_fresh import admit_base
from plangraph.spawner.prompt_input import checkout_review

argv = sys.argv[2:]
value = lambda flag: argv[argv.index(flag) + 1]
card = json.loads(value('--card'))
state = pathlib.Path(value('--state'))
record = dict(run_id='run-fixturereview', role='review', cwd=str(pathlib.Path.cwd()))
service = SimpleNamespace(
    result=lambda run_id: json.loads((state / 'runs' / run_id / 'record.json').read_text()),
    _save=lambda _: None,
)
def admit(launch=False):
    if asyncio.run(admit_base(service, record, card, launch=launch)):
        return
    print(json.dumps(record))
    sys.exit(1)
admit()
with tempfile.TemporaryDirectory() as checkout:
    head = checkout_review(card['diff_ref'], str(pathlib.Path(checkout) / 'tree'))
    record['review_source'] = card['diff_ref']['repo']
    admit(launch=True)
    assert record['checked_base_sha'] == head, record
    with (state / 'counted-seats').open('a') as log:
        print(head, file=log)
    record.update(status='completed', host='fixture', head_sha=head,
                  chosen_leg=dict(id='fixture', tool_mode='tools'),
                  stdout_tail=f'FILES READ: writer.txt:1\nHEAD: {head}\nVERDICT: ACCEPT\n')
print(json.dumps(record))
"#
            .into(),
            root.join("src").to_string_lossy().into_owned(),
        ];
        let origin = dir.path().join("origin.git");
        git_in(
            dir.path(),
            &["clone", "-q", origin.to_str().unwrap(), "train"],
        );
        let mut driver = MovingMain {
            host,
            upstream: dir.path().join("train"),
            conflict,
            race,
            original_head: String::new(),
        };
        let record = json!({"run_id": driver.host.run_id, "run_kind": "native-codex", "role": "implement",
                            "status": "running", "repo": driver.host.repo});
        let record = native_writer::run(&mut driver, record, "fixture brief");
        assert_eq!(record["exit_code"], 0, "{record}");
        let head = driver.host.head().unwrap();
        let main = git_in(&origin, &["rev-parse", "refs/heads/main"]);
        assert_eq!(record["review_base"]["observed_main"], main);
        assert_eq!(record["final_head"], head);
        assert_eq!(record["fast_checks"]["head"], head);
        assert_eq!(record["review"]["head"], head);
        assert_eq!(record["review_rounds"], 1);
        assert_eq!(
            fs::read_to_string(dir.path().join("counted-seats"))
                .unwrap()
                .trim(),
            head
        );
        assert_eq!(
            git_in(&origin, &["rev-parse", "refs/heads/phase/local-result"]),
            head
        );
        if conflict {
            assert_eq!(record["review_base"]["mode"], "conflict");
            assert_eq!(record["review_base"]["main"], record["admitted_base_sha"]);
            assert_eq!(head, driver.original_head);
            assert!(git_in(&driver.host.cwd, &["status", "--porcelain"]).is_empty());
        } else {
            assert_eq!(record["review_base"]["mode"], "rebased");
            assert_eq!(record["review_base"]["main"], main);
            assert_ne!(head, driver.original_head);
            assert_eq!(
                git_in(&driver.host.cwd, &["rev-parse", "HEAD^"]),
                record["base_sha"]
            );
            if lane_base {
                let base = record["base_sha"].as_str().unwrap();
                git_in(
                    &driver.host.cwd,
                    &["merge-base", "--is-ancestor", &main, base],
                );
                git_in(
                    &driver.host.cwd,
                    &[
                        "merge-base",
                        "--is-ancestor",
                        record["admitted_base_sha"].as_str().unwrap(),
                        base,
                    ],
                );
            } else {
                assert_eq!(record["base_sha"], main);
            }
        }
        assert_eq!(record["review_base_retry"].is_object(), race);
    }
}
