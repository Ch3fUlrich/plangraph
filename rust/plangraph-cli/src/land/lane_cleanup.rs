//! `plangraph lane-cleanup`: classify remote `lane/*` heads and delete the provably finished ones.
//!
//! Deleted classes: merged (ancestor of fresh main), in main by patch id (no merge commit, every
//! commit matched), superseded (a recorded replacement under one keeper batch key whose exact
//! refs and SHAs still hold and which preserves the old work). Stale heads are report-only.
//! Any unresolved protection (claims, open PRs as head or base, checkout, hold) keeps the head.
use super::lane_probe;
use pg_worker_core::{
    disk_hygiene::Guards,
    lane_delete::{self, run_git, Outcome},
    lockfile,
    run::record_io,
};
use serde::Deserialize;
use std::collections::BTreeSet;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const USAGE: &str = "usage:\n  plangraph lane-cleanup --repo DIR --state DIR --gh-repo OWNER/NAME [--remote NAME] [--claims-reader CMD] [--refs REF:SHA,...] [--stale-days N] [--apply]\n  plangraph lane-cleanup record-replacement --repo DIR --state DIR --batch-key KEY --old REF:SHA --new REF:SHA [--remote NAME]\n  plangraph lane-cleanup record-probe --state DIR --gh-repo OWNER/NAME --pr N --head SHA --run-id ID\n  plangraph lane-cleanup close-probes --state DIR --gh-repo OWNER/NAME [--apply]\nexit: 0 done or kept, 3 a delete was refused (retry), 2 usage or evidence error";

#[derive(Debug, PartialEq, Eq, Clone)]
pub enum Class {
    Merged,
    Patch,
    Superseded(String),
    Stale,
    Live,
}

impl Class {
    fn label(&self) -> String {
        match self {
            Class::Merged => "merged".into(),
            Class::Patch => "in-main-by-patch".into(),
            Class::Superseded(by) => format!("superseded-by:{by}"),
            Class::Stale => "stale".into(),
            Class::Live => "live".into(),
        }
    }
    fn deletable(&self) -> bool {
        matches!(self, Class::Merged | Class::Patch | Class::Superseded(_))
    }
}

#[derive(Deserialize, Default)]
#[serde(default)]
struct Claims {
    branches: Vec<String>,
}
#[derive(Deserialize)]
struct Pull {
    #[serde(rename = "headRefName")]
    head: String,
    #[serde(rename = "baseRefName")]
    base: String,
    #[serde(rename = "headRefOid")]
    sha: String,
}
#[derive(Deserialize, Clone)]
pub(super) struct Replacement {
    pub batch_key: String,
    pub old_ref: String,
    pub old_sha: String,
    pub new_ref: String,
    pub new_sha: String,
}

struct Cfg {
    repo: PathBuf,
    state: PathBuf,
    remote: String,
    gh_repo: String,
    claims_reader: Option<String>,
    refs: Option<Vec<(String, String)>>,
    /// Lane names (without `-rN`) whose every round is in scope.
    stems: Option<Vec<String>>,
    stale_days: u64,
    apply: bool,
}

pub fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

fn pair(text: &str) -> Result<(String, String), String> {
    let (reference, sha) = text.rsplit_once(':').ok_or("expected REF:SHA")?;
    if !lane_delete::is_lane_ref(reference) || !lane_delete::valid_sha(sha) {
        return Err(format!("not a lane ref and exact SHA: {text}"));
    }
    Ok((reference.into(), sha.into()))
}

pub fn run(args: &[String]) -> i32 {
    let result = match args.first().map(String::as_str) {
        Some("record-replacement") => record_replacement(&args[1..]).map(|_| 0),
        Some("record-probe") => lane_probe::record(&args[1..]).map(|_| 0),
        Some("close-probes") => lane_probe::close(&args[1..]),
        Some(first) if first.starts_with("--") => sweep(args),
        _ => Err(USAGE.into()),
    };
    match result {
        Ok(code) => code,
        Err(error) => {
            eprintln!("lane-cleanup: {error}");
            2
        }
    }
}

pub(super) fn state_dir(args: &[String]) -> Result<PathBuf, String> {
    let state = PathBuf::from(flag(args, "--state").ok_or(USAGE)?);
    record_io::ensure_private_dir(&state).map_err(|e| e.to_string())?;
    Ok(state)
}

fn ok(repo: &Path, args: &[&str]) -> Result<bool, String> {
    run_git(repo, args, 300)
        .map(|o| o.status.success())
        .map_err(|e| e.to_string())
}
fn out(repo: &Path, args: &[&str]) -> Result<String, String> {
    let o = run_git(repo, args, 300).map_err(|e| e.to_string())?;
    if !o.status.success() {
        return Err(format!("git {} failed", args.first().unwrap_or(&"")));
    }
    Ok(String::from_utf8_lossy(&o.stdout).into_owned())
}

/// Every non-main commit of `sha` is in main by patch id and no merge commit hides a resolution.
fn in_main_by_patch(repo: &Path, main: &str, sha: &str) -> Result<bool, String> {
    if !out(repo, &["rev-list", "--merges", &format!("{main}..{sha}")])?
        .trim()
        .is_empty()
    {
        return Ok(false);
    }
    Ok(out(repo, &["cherry", main, sha])?
        .lines()
        .all(|l| l.starts_with('-')))
}

/// The old branch's work is in the new branch or in main, commit by commit.
fn preserved_by(repo: &Path, main: &str, old: &str, new: &str) -> Result<bool, String> {
    if ok(repo, &["merge-base", "--is-ancestor", old, new])? {
        return Ok(true);
    }
    if !out(repo, &["rev-list", "--merges", &format!("{main}..{old}")])?
        .trim()
        .is_empty()
    {
        return Ok(false);
    }
    let plus = |upstream: &str| -> Result<BTreeSet<String>, String> {
        Ok(out(repo, &["cherry", upstream, old])?
            .lines()
            .filter_map(|l| l.strip_prefix("+ ").map(str::to_string))
            .collect())
    };
    Ok(plus(new)?.intersection(&plus(main)?).next().is_none())
}

fn stem(reference: &str) -> (&str, u32) {
    match reference.rsplit_once("-r") {
        Some((stem, n)) if !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()) => {
            (stem, n.parse().unwrap_or(0))
        }
        _ => (reference, 0),
    }
}

fn record_replacement(args: &[String]) -> Result<(), String> {
    let state = state_dir(args)?;
    let repo = PathBuf::from(flag(args, "--repo").ok_or(USAGE)?);
    let remote = flag(args, "--remote").unwrap_or_else(lane_delete::remote_name);
    let key = flag(args, "--batch-key")
        .filter(|k| !k.is_empty())
        .ok_or(USAGE)?;
    let (old_ref, old_sha) = pair(&flag(args, "--old").ok_or(USAGE)?)?;
    let (new_ref, new_sha) = pair(&flag(args, "--new").ok_or(USAGE)?)?;
    let (old_stem, old_n) = stem(&old_ref);
    let (new_stem, new_n) = stem(&new_ref);
    if old_stem != new_stem || new_n <= old_n {
        return Err("replacement must be a later round of the same lane".into());
    }
    refresh(&repo, &remote)?;
    for (reference, sha) in [(&old_ref, &old_sha), (&new_ref, &new_sha)] {
        let now = lane_delete::remote_sha(&repo, &remote, reference).map_err(|e| e.to_string())?;
        if now.as_deref() != Some(sha.as_str()) {
            return Err(format!("{reference} is not at {sha} on {remote}"));
        }
    }
    let main = format!("refs/remotes/{remote}/main");
    if !preserved_by(&repo, &main, &old_sha, &new_sha)? {
        return Err("the replacement does not preserve the old branch's work".into());
    }
    let row = serde_json::json!({"batch_key": key, "old_ref": old_ref, "old_sha": old_sha,
        "new_ref": new_ref, "new_sha": new_sha});
    append(&state.join("lane-replacements.jsonl"), &format!("{row}\n")).map_err(|e| e.to_string())
}

pub(super) fn append(path: &Path, text: &str) -> io::Result<()> {
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    file.write_all(text.as_bytes())?;
    file.sync_all()
}

fn refresh(repo: &Path, remote: &str) -> Result<(), String> {
    let main = format!("+refs/heads/main:refs/remotes/{remote}/main");
    let lanes = format!("+refs/heads/lane/*:refs/remotes/{remote}/lane/*");
    let o = run_git(
        repo,
        &[
            "fetch",
            "--prune",
            "--no-tags",
            "--quiet",
            remote,
            &main,
            &lanes,
        ],
        900,
    )
    .map_err(|e| e.to_string())?;
    if !o.status.success() {
        return Err("fetch of fresh main and lane heads failed".into());
    }
    Ok(())
}

fn remote_heads(repo: &Path, remote: &str) -> Result<Vec<(String, String)>, String> {
    let text = out(repo, &["ls-remote", remote, "refs/heads/lane/*"])?;
    Ok(text
        .lines()
        .filter_map(|l| l.split_once('\t'))
        .map(|(sha, name)| (name.to_string(), sha.to_string()))
        .collect())
}

fn replacements(state: &Path) -> Result<Vec<Replacement>, String> {
    match fs::read_to_string(state.join("lane-replacements.jsonl")) {
        Ok(text) => text
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| serde_json::from_str(l).map_err(|e| e.to_string()))
            .collect(),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(vec![]),
        Err(e) => Err(e.to_string()),
    }
}

/// Everything that keeps a head alive. `Err` means a source is unresolved: every head is kept.
struct Protection {
    claims: BTreeSet<String>,
    pulls: Vec<Pull>,
    checked_out: BTreeSet<String>,
    holds: BTreeSet<String>,
}

fn load_protection(cfg: &Cfg) -> Result<Protection, String> {
    let reader = cfg
        .claims_reader
        .as_ref()
        .ok_or("claims reader unavailable")?;
    let text = pg_worker_core::host_command::output(
        Command::new("sh")
            .env("PG_SWEEP_STATE", &cfg.state)
            .args(["-c", reader]),
        Duration::from_secs(120),
    )
    .map_err(|e| format!("claims reader failed: {e}"))?;
    let claims: Claims =
        serde_json::from_slice(&text).map_err(|e| format!("claims unreadable: {e}"))?;
    let listing = pg_worker_core::host_command::output(
        Command::new("gh").current_dir(&cfg.repo).args([
            "pr",
            "list",
            "--repo",
            &cfg.gh_repo,
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,headRefName,baseRefName,headRefOid",
        ]),
        Duration::from_secs(120),
    )
    .map_err(|e| format!("open PR inventory failed: {e}"))?;
    let pulls: Vec<Pull> = serde_json::from_slice(&listing).map_err(|e| e.to_string())?;
    if pulls.len() >= 1000 {
        return Err("open PR inventory truncated".into());
    }
    let tree = out(&cfg.repo, &["worktree", "list", "--porcelain"])?;
    let checked_out = tree
        .lines()
        .filter_map(|l| l.strip_prefix("branch ").map(str::to_string))
        .collect();
    let holds = match fs::read_to_string(cfg.state.join("lane-holds.txt")) {
        Ok(text) => text
            .lines()
            .map(|l| l.trim().to_string())
            .filter(|l| !l.is_empty())
            .collect(),
        Err(e) if e.kind() == io::ErrorKind::NotFound => BTreeSet::new(),
        Err(e) => return Err(e.to_string()),
    };
    Ok(Protection {
        claims: claims.branches.into_iter().collect(),
        pulls,
        checked_out,
        holds,
    })
}

impl Protection {
    fn reason(&self, remote: &str, reference: &str, sha: &str) -> Option<&'static str> {
        let short = reference.strip_prefix("refs/heads/").unwrap_or(reference);
        let remote_short = format!("{remote}/{short}");
        if Guards::protected_name(Path::new(reference)) {
            Some("protected-name")
        } else if self
            .claims
            .iter()
            .any(|b| b == short || b == reference || *b == remote_short)
        {
            Some("claimed")
        } else if self
            .pulls
            .iter()
            .any(|p| p.head == short || p.base == short || p.sha == sha)
        {
            Some("open-pr")
        } else if self.checked_out.contains(reference) {
            Some("checked-out")
        } else if self.holds.contains(reference) || self.holds.contains(short) {
            Some("held-for-settlement")
        } else {
            None
        }
    }
}

fn classify(
    cfg: &Cfg,
    main: &str,
    heads: &[(String, String)],
    reference: &str,
    sha: &str,
    records: &[Replacement],
) -> Result<Class, String> {
    let repo = cfg.repo.as_path();
    if !ok(repo, &["cat-file", "-e", &format!("{sha}^{{commit}}")])? {
        return Err("commit not fetched".into());
    }
    if ok(repo, &["merge-base", "--is-ancestor", sha, main])? {
        return Ok(Class::Merged);
    }
    if in_main_by_patch(repo, main, sha)? {
        return Ok(Class::Patch);
    }
    for rec in records
        .iter()
        .filter(|r| r.old_ref == reference && r.old_sha == sha)
    {
        let (old_stem, old_n) = stem(&rec.old_ref);
        let (new_stem, new_n) = stem(&rec.new_ref);
        let live = heads
            .iter()
            .any(|(r, s)| *r == rec.new_ref && *s == rec.new_sha);
        if live
            && old_stem == new_stem
            && new_n > old_n
            && !rec.batch_key.is_empty()
            && preserved_by(repo, main, sha, &rec.new_sha)?
        {
            return Ok(Class::Superseded(rec.new_ref.clone()));
        }
    }
    let age = out(repo, &["log", "-1", "--format=%ct", sha])?
        .trim()
        .parse::<u64>()
        .unwrap_or(0);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    Ok(if now.saturating_sub(age) > cfg.stale_days * 86400 {
        Class::Stale
    } else {
        Class::Live
    })
}

fn config(args: &[String]) -> Result<Cfg, String> {
    let refs = match flag(args, "--refs") {
        Some(list) => Some(
            list.split(',')
                .filter(|s| !s.is_empty())
                .map(pair)
                .collect::<Result<_, _>>()?,
        ),
        None => None,
    };
    let stems = flag(args, "--stems").map(|list| {
        list.split(',')
            .filter(|s| !s.is_empty())
            .map(|s| {
                stem(s.strip_prefix("refs/heads/").unwrap_or(s))
                    .0
                    .to_string()
            })
            .collect()
    });
    Ok(Cfg {
        stems,
        repo: PathBuf::from(flag(args, "--repo").ok_or(USAGE)?),
        state: state_dir(args)?,
        remote: flag(args, "--remote").unwrap_or_else(lane_delete::remote_name),
        gh_repo: flag(args, "--gh-repo").ok_or(USAGE)?,
        claims_reader: flag(args, "--claims-reader"),
        refs,
        stale_days: flag(args, "--stale-days")
            .and_then(|d| d.parse().ok())
            .unwrap_or(3),
        apply: args.iter().any(|a| a == "--apply"),
    })
}

/// The remote URL ends in `OWNER/NAME` on a path or host boundary.
fn names_repo(url: &str, gh_repo: &str) -> bool {
    let url = url.trim().trim_end_matches(".git");
    url.strip_suffix(gh_repo)
        .is_some_and(|rest| rest.ends_with('/') || rest.ends_with(':'))
}

fn sweep(args: &[String]) -> Result<i32, String> {
    let cfg = config(args)?;
    let _lock = lockfile::try_lock(&cfg.state.join("lane-cleanup.lock"))
        .map_err(|e| e.to_string())?
        .ok_or("another lane cleanup is running")?;
    // The push, the open-PR evidence and main must name one verified repository.
    let url = out(&cfg.repo, &["remote", "get-url", &cfg.remote])?;
    if !names_repo(&url, &cfg.gh_repo) {
        return Err(format!(
            "remote {} is not repository {}",
            cfg.remote, cfg.gh_repo
        ));
    }
    refresh(&cfg.repo, &cfg.remote)?;
    let main = out(
        &cfg.repo,
        &[
            "rev-parse",
            "--verify",
            &format!("refs/remotes/{}/main", cfg.remote),
        ],
    )?
    .trim()
    .to_string();
    let all = remote_heads(&cfg.repo, &cfg.remote)?;
    let heads: Vec<(String, String)> = if cfg.refs.is_none() && cfg.stems.is_none() {
        all.clone()
    } else {
        let exact = cfg.refs.as_deref().unwrap_or_default();
        let stems = cfg.stems.as_deref().unwrap_or_default();
        let name = |r: &str| {
            stem(r.strip_prefix("refs/heads/").unwrap_or(r))
                .0
                .to_string()
        };
        all.iter()
            .filter(|h| exact.contains(h) || stems.contains(&name(&h.0)))
            .cloned()
            .collect()
    };
    for (r, s) in cfg
        .refs
        .as_deref()
        .unwrap_or_default()
        .iter()
        .filter(|p| !heads.contains(p))
    {
        println!("lane {r} {s} absent-or-moved kept");
    }
    let protection = load_protection(&cfg);
    let records = replacements(&cfg.state)?;
    let (mut eligible, mut protected, mut deleted, mut absent, mut moved, mut refused, mut stale) =
        (0, 0, 0, 0, 0, 0, 0);
    let mut record_file: Option<PathBuf> = None;
    for (reference, sha) in &heads {
        let class = match classify(&cfg, &main, &all, reference, sha, &records) {
            Ok(class) => class,
            Err(why) => {
                println!("lane {reference} {sha} unproven kept ({why})");
                protected += 1;
                continue;
            }
        };
        if class == Class::Stale {
            stale += 1;
        }
        if !class.deletable() {
            println!("lane {reference} {sha} {} kept", class.label());
            continue;
        }
        eligible += 1;
        let blocked = match &protection {
            Ok(p) => p.reason(&cfg.remote, reference, sha).map(str::to_string),
            Err(why) => Some(format!("protection unresolved: {why}")),
        };
        if let Some(why) = blocked {
            println!("lane {reference} {sha} {} kept ({why})", class.label());
            protected += 1;
            continue;
        }
        if !cfg.apply {
            println!("lane {reference} {sha} {} would-delete", class.label());
            continue;
        }
        let file = match &record_file {
            Some(f) => f.clone(),
            None => {
                let dir = cfg.state.join("sweep");
                record_io::ensure_private_dir(&dir).map_err(|e| e.to_string())?;
                let ts = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_or(0, |d| d.as_nanos());
                let path = dir.join(format!("{ts}.tsv"));
                record_io::atomic_write_private(&path, b"repo\tpath\tbranch\tsha\tPR\n")
                    .map_err(|e| e.to_string())?;
                record_file = Some(path.clone());
                path
            }
        };
        match lane_delete::delete(&cfg.repo, &cfg.remote, reference, sha, &file) {
            Ok(Outcome::Deleted) => {
                deleted += 1;
                println!(
                    "lane {reference} {sha} {} deleted restore={}",
                    class.label(),
                    file.display()
                );
            }
            Ok(Outcome::Absent) => {
                absent += 1;
                println!("lane {reference} {sha} {} absent", class.label());
            }
            Ok(Outcome::Moved(now)) => {
                moved += 1;
                println!(
                    "lane {reference} {sha} {} moved-to {now} kept",
                    class.label()
                );
            }
            Err(error) => {
                refused += 1;
                println!("lane {reference} {sha} {} refused ({error})", class.label());
            }
        }
    }
    let remaining = all.len() - deleted - absent;
    println!(
        "reconcile heads={} eligible={eligible} protected={protected} deleted={deleted} absent={absent} moved={moved} refused={refused} stale_report_only={stale} remaining={remaining}",
        heads.len()
    );
    Ok(if refused > 0 { 3 } else { 0 })
}
