//! `plangraph lane-cleanup`: classify remote `lane/*` heads and delete the provably finished ones.
//!
//! Deleted classes: merged (ancestor of fresh main), in main (merging the head changes nothing),
//! superseded (a recorded replacement whose exact refs and SHAs still hold and which preserves
//! the old work). Stale heads are report-only. Any unresolved protection (claims, open PRs as head
//! or base, checkout, hold, repository binding) keeps the head.
use super::lane_guard::load_protection;
use super::lane_proof::{classify, out, preserved_by, stem, Class, Replacement};
use super::{lane_probe, lane_repo};
use pg_worker_core::{
    lane_delete::{self, run_git, Outcome},
    lockfile,
    run::record_io,
};
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const USAGE: &str = "usage:\n  plangraph lane-cleanup --repo DIR --state DIR --gh-repo OWNER/NAME [--gh-host HOST] [--remote NAME] [--claims-reader CMD] [--refs REF:SHA,...] [--stale-days N] [--apply]\n  plangraph lane-cleanup record-replacement --repo DIR --state DIR --batch-key KEY --old REF:SHA --new REF:SHA --gh-repo OWNER/NAME [--gh-host HOST] [--remote NAME]\n  plangraph lane-cleanup record-probe --state DIR --gh-repo OWNER/NAME --pr N --head SHA --run-id ID\n  plangraph lane-cleanup close-probes --state DIR --gh-repo OWNER/NAME [--apply]\nHOST defaults to github.com; `local` names a local-path remote (offline fixtures only).\nexit: 0 done or kept, 3 a delete was refused (retry), 2 usage or evidence error";

/// Protection data older than this is read again before the next delete.
const FRESH: Duration = Duration::from_secs(120);

struct Cfg {
    repo: PathBuf,
    state: PathBuf,
    remote: String,
    gh_repo: String,
    gh_host: String,
    claims_reader: Option<String>,
    refs: Option<Vec<(String, String)>>,
    stale_days: u64,
    apply: bool,
}

impl Cfg {
    fn gh_target(&self) -> String {
        lane_repo::gh_target(&self.gh_host, &self.gh_repo)
    }
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

fn gh_host(args: &[String]) -> String {
    flag(args, "--gh-host").unwrap_or_else(|| "github.com".into())
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
    let gh_repo = flag(args, "--gh-repo").ok_or(USAGE)?;
    lane_repo::verify(&repo, &remote, &gh_host(args), &gh_repo)?;
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
    let args = [
        "fetch",
        "--prune",
        "--no-tags",
        "--quiet",
        remote,
        &main,
        &lanes,
    ];
    let o = run_git(repo, &args, 900).map_err(|e| e.to_string())?;
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
    Ok(Cfg {
        repo: PathBuf::from(flag(args, "--repo").ok_or(USAGE)?),
        state: state_dir(args)?,
        remote: flag(args, "--remote").unwrap_or_else(lane_delete::remote_name),
        gh_repo: flag(args, "--gh-repo").ok_or(USAGE)?,
        gh_host: gh_host(args),
        claims_reader: flag(args, "--claims-reader"),
        refs,
        stale_days: flag(args, "--stale-days")
            .and_then(|d| d.parse().ok())
            .unwrap_or(3),
        apply: args.iter().any(|a| a == "--apply"),
    })
}

#[derive(Default)]
struct Tally {
    eligible: usize,
    protected: usize,
    deleted: usize,
    absent: usize,
    moved: usize,
    refused: usize,
    stale: usize,
}

fn restore_file(cfg: &Cfg, made: &mut Option<PathBuf>) -> Result<PathBuf, String> {
    if let Some(file) = made {
        return Ok(file.clone());
    }
    let dir = cfg.state.join("sweep");
    record_io::ensure_private_dir(&dir).map_err(|e| e.to_string())?;
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    let path = dir.join(format!("{ts}.tsv"));
    record_io::atomic_write_private(&path, b"repo\tpath\tbranch\tsha\tPR\n")
        .map_err(|e| e.to_string())?;
    *made = Some(path.clone());
    Ok(path)
}

fn sweep(args: &[String]) -> Result<i32, String> {
    let cfg = config(args)?;
    let _lock = lockfile::try_lock(&cfg.state.join("lane-cleanup.lock"))
        .map_err(|e| e.to_string())?
        .ok_or("another lane cleanup is running")?;
    // Main, the open-PR evidence and the push destination must be one verified repository.
    lane_repo::verify(&cfg.repo, &cfg.remote, &cfg.gh_host, &cfg.gh_repo)?;
    refresh(&cfg.repo, &cfg.remote)?;
    let main_ref = format!("refs/remotes/{}/main", cfg.remote);
    let main = out(&cfg.repo, &["rev-parse", "--verify", &main_ref])?
        .trim()
        .to_string();
    let all = remote_heads(&cfg.repo, &cfg.remote)?;
    // A train scope is the exact frozen refs and SHAs; nothing else enters the deletion set.
    let heads: Vec<(String, String)> = match &cfg.refs {
        None => all.clone(),
        Some(scope) => {
            for (r, s) in scope.iter().filter(|p| !all.contains(p)) {
                println!("lane {r} {s} absent-or-moved kept");
            }
            scope.iter().filter(|p| all.contains(p)).cloned().collect()
        }
    };
    let mut protection = load_protection(cfg.claims_reader.as_deref(), &cfg.state, &cfg.repo);
    let records = replacements(&cfg.state)?;
    let (mut tally, mut made) = (Tally::default(), None);
    for (reference, sha) in &heads {
        let proof = classify(
            &cfg.repo,
            &main,
            cfg.stale_days,
            &all,
            reference,
            sha,
            &records,
        );
        let class = match proof {
            Ok(class) => class,
            Err(why) => {
                println!("lane {reference} {sha} unproven kept ({why})");
                tally.protected += 1;
                continue;
            }
        };
        if class == Class::Stale {
            tally.stale += 1;
        }
        if !class.deletable() {
            println!("lane {reference} {sha} {} kept", class.label());
            continue;
        }
        tally.eligible += 1;
        if protection
            .as_ref()
            .map_or(true, |p| p.loaded.elapsed() > FRESH)
        {
            protection = load_protection(cfg.claims_reader.as_deref(), &cfg.state, &cfg.repo);
        }
        let blocked = match &protection {
            Err(why) => Some(format!("protection unresolved: {why}")),
            Ok(p) if p.unbound_runs > 0 && class != Class::Merged => Some(format!(
                "{} active runs have no bound publish ref",
                p.unbound_runs
            )),
            Ok(p) => p.reason(&cfg.remote, reference).map(str::to_string),
        };
        let label = class.label();
        if let Some(why) = blocked {
            println!("lane {reference} {sha} {label} kept ({why})");
            tally.protected += 1;
            continue;
        }
        let target = cfg.gh_target();
        match lane_delete::open_pr_use(&cfg.repo, Some(&target), reference) {
            Ok(None) => {}
            Ok(Some(why)) => {
                println!("lane {reference} {sha} {label} kept ({why})");
                tally.protected += 1;
                continue;
            }
            Err(error) => {
                println!(
                    "lane {reference} {sha} {label} kept (open PR evidence unavailable: {error})"
                );
                tally.protected += 1;
                continue;
            }
        }
        if !cfg.apply {
            println!("lane {reference} {sha} {label} would-delete");
            continue;
        }
        let file = restore_file(&cfg, &mut made)?;
        let result =
            lane_delete::delete(&cfg.repo, &cfg.remote, reference, sha, &file, Some(&target));
        match result {
            Ok(Outcome::Deleted) => {
                tally.deleted += 1;
                println!(
                    "lane {reference} {sha} {label} deleted restore={}",
                    file.display()
                );
            }
            Ok(Outcome::Absent) => {
                tally.absent += 1;
                println!("lane {reference} {sha} {label} absent");
            }
            Ok(Outcome::Moved(now)) => {
                tally.moved += 1;
                println!("lane {reference} {sha} {label} moved-to {now} kept");
            }
            Err(error) if error.to_string().starts_with("protected:") => {
                tally.protected += 1;
                println!("lane {reference} {sha} {label} kept ({error})");
            }
            Err(error) => {
                tally.refused += 1;
                println!("lane {reference} {sha} {label} refused ({error})");
            }
        }
    }
    println!(
        "reconcile heads={} eligible={} protected={} deleted={} absent={} moved={} refused={} stale_report_only={} remaining={}",
        heads.len(),
        tally.eligible,
        tally.protected,
        tally.deleted,
        tally.absent,
        tally.moved,
        tally.refused,
        tally.stale,
        all.len() - tally.deleted - tally.absent
    );
    Ok(if tally.refused > 0 { 3 } else { 0 })
}
