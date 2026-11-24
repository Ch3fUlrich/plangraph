//! The gate a completed writer head must pass before it is reported: the repository's
//! seconds-fast checks and private scrub over `base..head`, and the rule that every
//! `Plan-Item` trailer names an item of the writer's brief. Claude batch completion runs it
//! through `pg-spawnd head-gate`; it is the same list of gates the native Codex driver runs.

use crate::lane_guard::item_keys;
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};

/// One completed head to check.
pub struct Gate<'a> {
    pub worktree: &'a Path,
    /// The admitted base the writer was launched on.
    pub base: &'a str,
    pub head: &'a str,
    /// The writer brief; its item keys bound the `Plan-Item` trailers.
    pub brief: &'a str,
    /// The private scrub words file; the scrub is skipped when absent.
    pub words: Option<PathBuf>,
}

/// The default private scrub words file of the user, when it exists.
pub fn scrub_words() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .map(|home| Path::new(&home).join(".config/plangraph/scrub-words.txt"))
        .filter(|path| path.is_file())
}

fn git(worktree: &Path, args: &[&str]) -> Result<String, String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(worktree)
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!(
            "git {}: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_owned())
}

fn check_item_trailers(gate: &Gate) -> Result<(), String> {
    let assigned = item_keys(gate.brief);
    let range = format!("{}..{}", gate.base, gate.head);
    let log = git(
        gate.worktree,
        &[
            "log",
            "-z",
            "--format=%H%x00%(trailers:key=Plan-Item,valueonly,unfold)",
            &range,
            "--",
        ],
    )?;
    let mut records = log.split_terminator('\0');
    while let (Some(commit), Some(trailers)) = (records.next(), records.next()) {
        for item in trailers.lines().map(str::trim).filter(|i| !i.is_empty()) {
            if !assigned.iter().any(|key| key == item) {
                return Err(format!(
                    "commit {}: Plan-Item outside writer brief: {item}",
                    commit.trim()
                ));
            }
        }
    }
    Ok(())
}

/// Run the gates on the completed head; `Ok` is the combined output of the passing gates,
/// `Err` the first failing gate with both of its streams.
pub fn run(gate: &Gate) -> Result<String, String> {
    if git(gate.worktree, &["rev-parse", "HEAD"])? != gate.head {
        return Err(format!("worktree is not at {}", gate.head));
    }
    check_item_trailers(gate)?;
    let range = |extra: &[&str]| -> Vec<String> {
        extra
            .iter()
            .map(|s| s.to_string())
            .chain([
                "--base".into(),
                gate.base.into(),
                "--head".into(),
                "HEAD".into(),
            ])
            .collect()
    };
    let mut gates: Vec<(&str, Vec<String>)> = vec![
        ("check_file_size.py", vec![]),
        ("check_new_code_lang.py", vec![]),
        ("check_changelog_docs.py", vec![]),
        ("ci_commit_gate.py", range(&["check"])),
    ];
    if let Some(words) = &gate.words {
        let mut args = range(&[]);
        args.extend(["--words".into(), words.to_string_lossy().into_owned()]);
        gates.push(("preflight_private_scrub.py", args));
    }
    let mut log = String::new();
    for (name, args) in gates {
        let path = gate.worktree.join("tools").join(name);
        if !path.is_file() {
            continue;
        }
        let mut command = Command::new(if cfg!(windows) { "python" } else { "python3" });
        command.arg(path).args(args).current_dir(gate.worktree);
        if name == "check_new_code_lang.py" {
            command.env("CLASS_SEARCH_BASE", gate.base);
        }
        let result = command.output().map_err(|e| format!("{name}: {e}"))?;
        let (stdout, stderr) = (
            String::from_utf8_lossy(&result.stdout),
            String::from_utf8_lossy(&result.stderr),
        );
        if !result.status.success() {
            return Err(format!(
                "{name}: command failed ({}):\nstdout: {}\nstderr: {}",
                result.status,
                stdout.trim(),
                stderr.trim()
            ));
        }
        log.push_str(&format!(
            "== {name} ({})\n{stdout}{stderr}\n",
            result.status
        ));
    }
    Ok(log)
}

/// The record of one gate run: the head, the verdict and the gate's own output.
pub fn diagnostic(gate: &Gate, result: &Result<String, String>) -> Value {
    json!({
        "head": gate.head, "base": gate.base, "gate": "head-gate",
        "ok": result.is_ok(),
        "diagnostic": match result { Ok(log) | Err(log) => log },
    })
}

/// `pg-spawnd head-gate --worktree DIR --base SHA --head SHA --brief FILE [--words FILE]
/// [--record FILE]`: exit 0 when the head passes, 1 with the failing gate on stderr otherwise.
/// The diagnostic is also written to `--record` when given.
pub fn execute(args: Vec<String>, out: &mut dyn std::io::Write) -> i32 {
    let mut values = std::collections::BTreeMap::new();
    let mut it = args.into_iter();
    while let Some(flag) = it.next() {
        let name = flag.strip_prefix("--").map(str::to_owned);
        match (name, it.next()) {
            (Some(name), Some(value)) => values.insert(name, value),
            _ => {
                eprintln!("head-gate: usage: --worktree DIR --base SHA --head SHA --brief FILE [--words FILE] [--record FILE]");
                return 2;
            }
        };
    }
    let (Some(worktree), Some(base), Some(head), Some(brief)) = (
        values.get("worktree"),
        values.get("base"),
        values.get("head"),
        values.get("brief"),
    ) else {
        eprintln!("head-gate: --worktree, --base, --head and --brief are required");
        return 2;
    };
    let brief = match fs::read_to_string(brief) {
        Ok(brief) => brief,
        Err(error) => {
            eprintln!("head-gate: brief unreadable: {error}");
            return 2;
        }
    };
    let gate = Gate {
        worktree: Path::new(worktree),
        base,
        head,
        brief: &brief,
        words: values.get("words").map(PathBuf::from).or_else(scrub_words),
    };
    let result = run(&gate);
    let record = diagnostic(&gate, &result);
    if let Some(path) = values.get("record") {
        if let Err(error) = fs::write(path, format!("{record}\n")) {
            eprintln!("head-gate: record unwritable: {error}");
            return 2;
        }
    }
    let _ = writeln!(out, "{record}");
    if let Err(reason) = &result {
        eprintln!("head-gate: rejected {head}: {reason}");
    }
    i32::from(result.is_err())
}

#[cfg(all(test, unix))]
mod tests {
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
        assert!(
            log.contains("== check_file_size.py") && log.contains("== preflight_private_scrub.py")
        );
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
        fs::write(&brief, BRIEF).unwrap();
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
}
