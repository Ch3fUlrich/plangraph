use super::logs::LOG_CAP;
use serde_json::Value;
use std::io::Read;
use std::process::{Command, Stdio};

pub trait Github {
    fn pages(&mut self, endpoint: &str, field: Option<&str>) -> Result<Vec<Value>, String>;
    /// First page only (newest first), one REST call.
    fn page(&mut self, endpoint: &str, field: Option<&str>) -> Result<Vec<Value>, String>;
    fn log(&mut self, job: u64) -> Result<String, String>;
    fn rerun(&mut self, run: u64) -> Result<(), String>;
    /// `gh run <verb> <run>`: verb is `rerun-failed`, `rerun` or `cancel`.
    fn run_verb(&mut self, _run: u64, _verb: &str) -> Result<(), String> {
        Err("run verbs are not supported here".into())
    }
    /// One JSON object (not a row list), one REST call.
    fn value(&mut self, _endpoint: &str) -> Result<Value, String> {
        Err("single-object reads are not supported here".into())
    }
}
pub struct Gh {
    pub repo: String,
}

pub fn command(program: &str, args: &[&str]) -> Result<String, String> {
    let output = Command::new(program)
        .args(args)
        .output()
        .map_err(|_| format!("cannot execute {program}"))?;
    if !output.status.success() {
        return Err(format!("{program} request failed (details suppressed)"));
    }
    String::from_utf8(output.stdout).map_err(|_| "non-UTF8 response".into())
}

pub fn repository(remote: &str) -> Result<String, String> {
    let path = remote.trim().trim_end_matches('/').trim_end_matches(".git");
    let path = path.rsplit_once(':').map_or(path, |(_, p)| p);
    let parts: Vec<_> = path.split('/').collect();
    if parts.len() < 2 {
        return Err("origin must name an owner/repository".into());
    }
    let repo = parts[parts.len() - 2..].join("/");
    if !repo
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"-._/".contains(&b))
    {
        return Err("invalid origin repository".into());
    }
    Ok(repo)
}

pub(super) fn capped_log(reader: impl Read) -> std::io::Result<(Vec<u8>, bool)> {
    let mut bytes = Vec::new();
    reader.take((LOG_CAP + 1) as u64).read_to_end(&mut bytes)?;
    let capped = bytes.len() > LOG_CAP;
    bytes.truncate(LOG_CAP);
    Ok((bytes, capped))
}

pub(super) fn parse_pages(raw: &str, field: Option<&str>) -> Result<Vec<Value>, String> {
    // --paginate emits successive JSON values, including on older gh releases
    // that do not support --slurp.
    let pages = serde_json::Deserializer::from_str(raw).into_iter::<Value>();
    let mut result = Vec::new();
    for page in pages {
        let page = page.map_err(|_| "invalid gh JSON")?;
        let rows = field
            .map_or(&page, |f| &page[f])
            .as_array()
            .ok_or("missing gh rows")?;
        result.extend(rows.iter().cloned());
    }
    Ok(result)
}

impl Github for Gh {
    fn pages(&mut self, endpoint: &str, field: Option<&str>) -> Result<Vec<Value>, String> {
        let raw = command(
            "gh",
            &[
                "api",
                "--paginate",
                &format!("repos/{}/{endpoint}", self.repo),
            ],
        )?;
        parse_pages(&raw, field)
    }
    fn page(&mut self, endpoint: &str, field: Option<&str>) -> Result<Vec<Value>, String> {
        let raw = command("gh", &["api", &format!("repos/{}/{endpoint}", self.repo)])?;
        parse_pages(&raw, field)
    }
    fn log(&mut self, job: u64) -> Result<String, String> {
        let endpoint = format!("repos/{}/actions/jobs/{job}/logs", self.repo);
        let mut child = Command::new("gh")
            .args(["api", &endpoint])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "cannot read job log")?;
        let read = capped_log(child.stdout.take().unwrap());
        if read.as_ref().map_or(true, |(_, capped)| *capped) {
            let _ = child.kill();
        }
        let status = child.wait().map_err(|_| "log reader wait failed")?;
        let (bytes, capped) = read.map_err(|_| "log reader failed")?;
        if !capped && !status.success() {
            return Err("gh job log request failed".into());
        }
        let mut log = String::from_utf8_lossy(&bytes).into_owned();
        if capped {
            log.push_str("\n[log truncated at 1 MiB]");
        }
        Ok(log)
    }
    fn run_verb(&mut self, run: u64, verb: &str) -> Result<(), String> {
        let run = run.to_string();
        let args: Vec<&str> = match verb {
            "rerun-failed" => vec!["run", "rerun", &run, "--failed", "--repo", &self.repo],
            "rerun" => vec!["run", "rerun", &run, "--repo", &self.repo],
            "cancel" => vec!["run", "cancel", &run, "--repo", &self.repo],
            _ => return Err("refused: unknown run verb".into()),
        };
        let out = Command::new("gh")
            .args(&args)
            .output()
            .map_err(|_| "unknown outcome: cannot execute gh".to_owned())?;
        if out.status.success() {
            return Ok(());
        }
        // An HTTP 4xx answer is GitHub's refusal; anything else (transport, a lost reply) is unknown.
        let stderr = String::from_utf8_lossy(&out.stderr);
        Err(if stderr.contains("HTTP 4") {
            "refused: GitHub answered with a client error".into()
        } else {
            "unknown outcome: gh failed without a definitive answer".into()
        })
    }
    fn value(&mut self, endpoint: &str) -> Result<Value, String> {
        let raw = command("gh", &["api", &format!("repos/{}/{endpoint}", self.repo)])?;
        serde_json::from_str(&raw).map_err(|_| "invalid gh JSON".into())
    }
    fn rerun(&mut self, run: u64) -> Result<(), String> {
        command(
            "gh",
            &[
                "run",
                "rerun",
                &run.to_string(),
                "--failed",
                "--repo",
                &self.repo,
            ],
        )
        .map(|_| ())
    }
}
