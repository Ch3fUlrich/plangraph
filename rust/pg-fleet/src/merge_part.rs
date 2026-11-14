use crate::executor::{strings, Executor};
use crate::process::Output;
use std::path::PathBuf;

pub struct Options {
    pub pr: String,
    pub want: String,
    pub key: String,
    pub go: String,
    pub subject: String,
    pub body: PathBuf,
    pub repo: String,
    pub session: Option<String>,
    pub no_claim: bool,
    pub keeper: String,
    pub evidence: String,
    pub gate: String,
    pub now: String,
    pub sleep: Option<u64>,
}
#[derive(Default)]
pub struct Result {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}
impl Result {
    fn line(&mut self, s: &str) {
        self.stdout.push_str(s);
        self.stdout.push('\n');
    }
    fn stop(mut self, code: i32, reason: &str) -> Self {
        self.code = code;
        self.line(&format!("STOP: {reason}"));
        self
    }
}
fn call(r: &mut dyn Executor, p: &str, a: &[&str], timeout: u64, combined: bool) -> Output {
    r.run(p, &strings(a), timeout, combined)
        .unwrap_or_else(|e| Output {
            code: if e == "TimeoutExpired" { 124 } else { 127 },
            stdout: String::new(),
            stderr: String::new(),
        })
}
pub fn merge(o: &Options, r: &mut dyn Executor) -> Result {
    let mut result = Result::default();
    let view = |r: &mut dyn Executor, field: &str, jq: &str| {
        call(
            r,
            "gh",
            &[
                "pr", "view", &o.pr, "--repo", &o.repo, "--json", field, "--jq", jq,
            ],
            120,
            false,
        )
    };
    let head = view(r, "headRefOid", ".headRefOid");
    result.stderr += &head.stderr;
    let head = head.stdout.trim_end_matches('\n');
    if head.is_empty() || o.want.is_empty() {
        return result.stop(2, "empty head or HEAD_PREFIX");
    }
    if !head.starts_with(&o.want) {
        return result.stop(
            2,
            &format!("head moved to {head} (GO {} names {})", o.go, o.want),
        );
    }
    let mut mergeable = String::new();
    for _ in 0..6 {
        let out = view(r, "mergeable", ".mergeable");
        result.stderr += &out.stderr;
        mergeable = out.stdout.trim_end_matches('\n').to_owned();
        if mergeable != "UNKNOWN" {
            break;
        }
        r.pause(o.sleep.unwrap_or(10));
    }
    let session = o.session.clone().unwrap_or_else(|| {
        call(r, "fleet-name", &["plangraph-replan"], 30, false)
            .stdout
            .trim_end_matches('\n')
            .to_owned()
    });
    let gate = call(
        r,
        &o.gate,
        &[
            "--go",
            &o.go,
            "--head",
            head,
            "--session",
            &session,
            "--key",
            &o.key,
        ],
        120,
        true,
    );
    result.line(gate.stdout.trim_end_matches('\n'));
    if gate.code != 0 {
        return result.stop(5, &format!("go-row-gate rc={}", gate.code));
    }
    let show = call(r, "bash", &[&o.keeper, "show", &o.key], 10, false);
    if show.code == 124 {
        return result.stop(6, &format!("keeper timed out {}", o.key));
    }
    if show.code != 0 {
        return result.stop(6, &format!("no keeper item {}", o.key));
    }
    if mergeable != "MERGEABLE" {
        return result.stop(
            3,
            &format!("mergeable={mergeable} (main merge + push + CI needed)"),
        );
    }
    let body = std::fs::read_to_string(&o.body).unwrap_or_default();
    if !body
        .split('\n')
        .any(|s| s == format!("Plan-Item: {}", o.key))
    {
        return result.stop(4, &format!("body has no Plan-Item {}", o.key));
    }
    if !o.no_claim {
        let mut claim = call(r, "bash", &[&o.keeper, "claim", &o.key], 10, true);
        if claim.code == 124 {
            return result.stop(8, &format!("keeper timed out {}", o.key));
        }
        let tail = claim.stdout.lines().last().unwrap_or("");
        if tail.contains("stale") {
            claim = call(
                r,
                "bash",
                &[
                    &o.keeper,
                    "claim",
                    "--force-stale",
                    &format!("GO {} names this item at {}", o.go, o.want),
                    &o.key,
                ],
                10,
                true,
            );
        }
        if claim.code == 124 {
            return result.stop(8, &format!("keeper timed out {}", o.key));
        }
        result.line(
            &claim
                .stdout
                .lines()
                .last()
                .unwrap_or("")
                .chars()
                .take(160)
                .collect::<String>(),
        );
        if claim.code != 0 {
            return result.stop(8, &format!("claim failed rc={}", claim.code));
        }
    }
    for attempt in 1..=5 {
        let out = call(
            r,
            "gh",
            &[
                "api",
                "-X",
                "PUT",
                &format!("repos/{}/pulls/{}/merge", o.repo, o.pr),
                "-f",
                "merge_method=merge",
                "-f",
                &format!("sha={head}"),
                "-f",
                &format!("commit_title={}", o.subject),
                "-F",
                &format!("commit_message=@{}", o.body.display()),
                "--jq",
                ".sha",
            ],
            120,
            false,
        );
        result.stdout += &out.stdout;
        result.stderr += &out.stderr;
        if out.code == 0 {
            break;
        }
        r.pause(o.sleep.unwrap_or(attempt * 20));
        let state = view(r, "state", ".state");
        result.stderr += &state.stderr;
        if state.stdout.trim_end_matches('\n') == "MERGED" {
            break;
        }
        if attempt == 5 {
            return result.stop(7, "merge failed");
        }
    }
    r.pause(o.sleep.unwrap_or(5));
    let sha = view(r, "mergeCommit", ".mergeCommit.oid");
    result.stderr += &sha.stderr;
    let sha = sha.stdout.trim_end_matches('\n');
    result.line(&format!("MERGED {} #{} {sha} (GO {})", o.now, o.pr, o.go));
    if !o.no_claim {
        r.pause(o.sleep.unwrap_or(20));
        let out = call(r, "bash", &[&o.evidence, sha, &o.key], 600, false);
        result.stdout += &out.stdout;
        result.stderr += &out.stderr;
        result.code = out.code;
    }
    result
}

pub fn main(args: &[String]) -> i32 {
    let no_claim = args.first().is_some_and(|s| s == "--no-claim");
    let args = &args[usize::from(no_claim)..];
    if args.len() != 6 {
        eprintln!(
            "usage: merge-part [--no-claim] PR HEAD_PREFIX ITEM_KEY GO_DID SUBJECT BODY_FILE"
        );
        return 1;
    }
    let keeper = match crate::merge_paths::lookup("pgk") {
        Ok(p) => p,
        Err(e) => {
            println!("STOP: {e}");
            return 6;
        }
    };
    let evidence = match crate::merge_paths::lookup("evidence") {
        Ok(p) => p,
        Err(e) => {
            println!("STOP: {e}");
            return 6;
        }
    };
    let gate = std::env::current_exe()
        .ok()
        .map(|p| {
            p.with_file_name(if cfg!(windows) {
                "go-row-gate.exe"
            } else {
                "go-row-gate"
            })
        })
        .filter(|p| p.is_file())
        .map_or("go-row-gate".into(), |p| crate::paths::display(&p));
    let repo = match std::env::var("PG_REPO") {
        Ok(repo) if !repo.trim().is_empty() => repo,
        _ => {
            println!("STOP: PG_REPO must name the target repository");
            return 1;
        }
    };
    let o = Options {
        pr: args[0].clone(),
        want: args[1].clone(),
        key: args[2].clone(),
        go: args[3].clone(),
        subject: args[4].clone(),
        body: args[5].clone().into(),
        repo,
        session: std::env::var("FLEET_SESSION_NAME").ok(),
        no_claim,
        keeper: crate::paths::display(&keeper),
        evidence: crate::paths::display(&evidence),
        gate,
        now: chrono::Utc::now().format("%H:%MZ").to_string(),
        sleep: std::env::var("PG_MERGE_SLEEP")
            .ok()
            .and_then(|s| s.parse().ok()),
    };
    let result = merge(&o, &mut crate::executor::Local);
    print!("{}", result.stdout);
    eprint!("{}", result.stderr);
    result.code
}
