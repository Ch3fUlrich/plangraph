//! The effects of `land_go` over `gh`, `tools/merge_batch.py land`, a post command and the keeper CLI.
use crate::land_go::{pass, GoRow, Landing};
use std::{io::Write, path::PathBuf, process::Command};

const USAGE: &str = "land-go --rows FILE --done FILE --repo OWNER/NAME --checkout DIR --post-cmd CMD [--keeper-cmd CMD]";

struct Shell {
    repo: String,
    checkout: PathBuf,
    post_cmd: Vec<String>,
    keeper_cmd: Vec<String>,
}

fn run(program: &str, args: &[&str], dir: Option<&PathBuf>) -> Result<String, String> {
    let mut cmd = Command::new(program);
    cmd.args(args);
    if let Some(dir) = dir {
        cmd.current_dir(dir);
    }
    let out = cmd.output().map_err(|e| format!("{program}: {e}"))?;
    let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if out.status.success() {
        return Ok(text);
    }
    let err = String::from_utf8_lossy(&out.stderr);
    let first = err
        .lines()
        .chain(text.lines())
        .find(|l| !l.trim().is_empty())
        .unwrap_or("no output");
    Err(format!(
        "{program} exits {}: {}",
        out.status.code().unwrap_or(-1),
        first.trim()
    ))
}

impl Landing for Shell {
    fn pr_head(&self, pr: u64) -> Result<String, String> {
        run(
            "gh",
            &[
                "pr",
                "view",
                &pr.to_string(),
                "--repo",
                &self.repo,
                "--json",
                "headRefOid",
                "-q",
                ".headRefOid",
            ],
            None,
        )
    }
    fn behind_by(&self, head: &str) -> Result<u64, String> {
        let path = format!("repos/{}/compare/main...{head}", self.repo);
        run("gh", &["api", &path, "--jq", ".behind_by"], None)?
            .parse()
            .map_err(|_| "behind_by is not a number".to_string())
    }
    fn checks_green(&self, pr: u64) -> Result<(), String> {
        run(
            "gh",
            &[
                "pr",
                "checks",
                &pr.to_string(),
                "--repo",
                &self.repo,
                "--required",
            ],
            None,
        )
        .map(|_| ())
    }
    fn land(&self, row: &GoRow) -> Result<String, String> {
        let pr = row.pr.to_string();
        let branch = run(
            "gh",
            &[
                "pr",
                "view",
                &pr,
                "--repo",
                &self.repo,
                "--json",
                "headRefName",
                "-q",
                ".headRefName",
            ],
            None,
        )?;
        let name = branch
            .strip_prefix("batch/")
            .ok_or("the pull request head is not a batch/ branch")?;
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let approval = dir.path().join("approval");
        std::fs::write(
            &approval,
            format!("{}\nHEAD: {}\nVERDICT: PASS\n", row.text, row.head),
        )
        .map_err(|e| e.to_string())?;
        let out = run(
            "python3",
            &[
                "tools/merge_batch.py",
                "land",
                "--name",
                name,
                "--batch-pr",
                &pr,
                "--approval",
                &approval.to_string_lossy(),
            ],
            Some(&self.checkout),
        )?;
        let merged = out.lines().find_map(|l| {
            let mut t = l.split_whitespace();
            (t.next() == Some("MERGED"))
                .then(|| t.nth(1).map(str::to_string))
                .flatten()
        });
        merged.ok_or_else(|| "land printed no MERGED line".to_string())
    }
    fn post(&self, line: &str) -> Result<(), String> {
        let (program, args) = self.post_cmd.split_first().ok_or("no post command")?;
        let mut args: Vec<&str> = args.iter().map(String::as_str).collect();
        args.push(line);
        run(program, &args, None).map(|_| ())
    }
    fn close_merged_dry_run(&self, key: &str, decision: &str) -> Result<String, String> {
        let (program, args) = self.keeper_cmd.split_first().ok_or("no keeper command")?;
        let mut args: Vec<&str> = args.iter().map(String::as_str).collect();
        args.extend(["close-merged", key, decision, "--from-open"]);
        let out = run(program, &args, None)?;
        out.lines()
            .nth(1)
            .map(str::to_string)
            .ok_or_else(|| "no result line".to_string())
    }
}

/// One pass over the GO rows; the timer that runs it is the watch.
pub(crate) fn execute(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    if args.first().is_some_and(|a| a == "--help" || a == "-h") {
        return writeln!(out, "{USAGE}").map_err(|e| e.to_string());
    }
    let mut flags = std::collections::BTreeMap::new();
    let mut it = args.into_iter();
    while let Some(flag) = it.next() {
        let value = it
            .next()
            .filter(|v| !v.starts_with("--"))
            .ok_or("missing land-go flag value")?;
        if ![
            "--rows",
            "--done",
            "--repo",
            "--checkout",
            "--post-cmd",
            "--keeper-cmd",
        ]
        .contains(&flag.as_str())
            || flags.insert(flag, value).is_some()
        {
            return Err("unknown or duplicate land-go flag".into());
        }
    }
    let get = |k: &str| flags.get(k).cloned().ok_or(format!("missing {k}"));
    let words = |s: String| s.split_whitespace().map(str::to_string).collect::<Vec<_>>();
    let shell = Shell {
        repo: get("--repo")?,
        checkout: PathBuf::from(get("--checkout")?),
        post_cmd: words(get("--post-cmd")?),
        keeper_cmd: words(
            flags
                .get("--keeper-cmd")
                .cloned()
                .unwrap_or_else(|| "plangraph-keeper".into()),
        ),
    };
    pass(
        &PathBuf::from(get("--rows")?),
        &PathBuf::from(get("--done")?),
        &shell,
        out,
    )
    .map(|_| ())
}
