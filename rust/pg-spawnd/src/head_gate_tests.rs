use super::*;

fn git_in(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(["-c", "user.name=t", "-c", "user.email=t@example.test"])
        .args(args)
        .output()
        .unwrap();
    assert!(out.status.success(), "{args:?}: {out:?}");
    String::from_utf8_lossy(&out.stdout).trim().to_owned()
}

const SCRUB: &str = "import sys\nsys.exit('scrub hit: fixture' if 'private' in open(sys.argv[sys.argv.index('--words') + 1]).read() else 0)\n";

struct Fixture {
    dir: tempfile::TempDir,
    base: String,
    words: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        git_in(repo, &["init", "-q", "-b", "main"]);
        fs::create_dir_all(repo.join("tools")).unwrap();
        fs::write(repo.join("tools/preflight_private_scrub.py"), SCRUB).unwrap();
        fs::write(repo.join("tools/check_file_size.py"), "print('size ok')\n").unwrap();
        git_in(repo, &["add", "."]);
        git_in(repo, &["commit", "-q", "-m", "base"]);
        let base = git_in(repo, &["rev-parse", "HEAD"]);
        let words = repo.join("words.txt");
        fs::write(&words, "private\n").unwrap();
        Self { dir, base, words }
    }

    fn commit(&self, message: &str) -> String {
        git_in(
            self.dir.path(),
            &["commit", "-q", "--allow-empty", "-m", message],
        );
        git_in(self.dir.path(), &["rev-parse", "HEAD"])
    }

    fn gate<'a>(&'a self, head: &'a str, brief: &'a str) -> Gate<'a> {
        Gate {
            worktree: self.dir.path(),
            base: &self.base,
            head,
            brief,
            words: Some(self.words.clone()),
        }
    }
}

const BRIEF: &str = "### demo:ASSIGNED | t\nWhy: x\n";

#[test]
fn a_head_with_a_scrub_hit_is_rejected_with_the_gate_output() {
    let f = Fixture::new();
    let head = f.commit("work\n\nPlan-Item: demo:ASSIGNED");
    let result = run(&f.gate(&head, BRIEF));
    let error = result.clone().unwrap_err();
    assert!(
        error.starts_with("preflight_private_scrub.py: command failed"),
        "{error}"
    );
    assert!(error.contains("scrub hit: fixture"), "{error}");
    let record = diagnostic(&f.gate(&head, BRIEF), &result);
    assert_eq!(record["ok"], false);
    assert_eq!(record["head"], json!(head));
    assert!(record["diagnostic"].as_str().unwrap().contains("scrub hit"));
}

#[test]
fn a_clean_head_passes_and_the_log_names_every_gate_that_ran() {
    let f = Fixture::new();
    fs::write(&f.words, "nothing\n").unwrap();
    let head = f.commit("work\n\nPlan-Item: demo:ASSIGNED");
    let log = run(&f.gate(&head, BRIEF)).unwrap();
    assert!(log.contains("== check_file_size.py") && log.contains("== preflight_private_scrub.py"));
    assert!(diagnostic(&f.gate(&head, BRIEF), &Ok(log))["ok"] == true);
}

#[test]
fn a_plan_item_outside_the_brief_and_a_moved_head_are_rejected() {
    let f = Fixture::new();
    let head = f.commit("work\n\nPlan-Item: demo:OTHER");
    assert_eq!(
        run(&f.gate(&head, BRIEF)).unwrap_err(),
        format!("commit {head}: Plan-Item outside writer brief: demo:OTHER")
    );
    let moved = f.commit("later");
    let error = run(&f.gate(&head, BRIEF)).unwrap_err();
    assert_eq!(error, format!("worktree is not at {head}"));
    assert_ne!(moved, head);
}

#[test]
fn the_cli_writes_the_diagnostic_record_and_exits_nonzero_on_a_scrub_hit() {
    let f = Fixture::new();
    let head = f.commit("work\n\nPlan-Item: demo:ASSIGNED");
    let brief = f.dir.path().join("brief.md");
    // A typed batch input names its items as `### key | title` once rendered.
    let typed = json!({"items":[{"key":"demo:ASSIGNED","title":"t","brief":"Why: x"}],
            "run":{"name":"n","worktree":"w","base":f.base,"writer_family":"claude",
                   "status_file":"s","fast_checks":["true"],"review_cmd":"r",
                   "review_wait_seconds":1,"round_cap":1,"trailers":["Plan-Item: demo:ASSIGNED"]}});
    fs::write(&brief, format!("PLANGRAPH-BATCH-INPUT-V1\n{typed}")).unwrap();
    let record = f.dir.path().join("gate.json");
    let args = [
        "--worktree",
        f.dir.path().to_str().unwrap(),
        "--base",
        &f.base,
        "--head",
        &head,
        "--brief",
        brief.to_str().unwrap(),
        "--words",
        f.words.to_str().unwrap(),
        "--record",
        record.to_str().unwrap(),
    ];
    let mut out = Vec::new();
    let code = execute(args.iter().map(|s| s.to_string()).collect(), &mut out);
    assert_eq!(code, 1);
    let saved: Value = serde_json::from_str(&fs::read_to_string(&record).unwrap()).unwrap();
    assert_eq!(saved["ok"], false);
    assert!(saved["diagnostic"].as_str().unwrap().contains("scrub hit"));
    assert_eq!(execute(vec!["--worktree".into()], &mut out), 2);
}
