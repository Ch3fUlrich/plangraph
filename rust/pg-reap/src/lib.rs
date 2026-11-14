use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Facts {
    pub idle_hours: u64,
    pub clean: bool,
    pub merged_into_main: bool,
    /// Every commit reachable from HEAD is present on a remote; no unpushed commits.
    pub pushed: bool,
    pub failed: bool,
    pub has_claim: bool,
    pub live_run: bool,
    pub open_pr: bool,
    pub in_use_by_process: bool,
    pub in_use_by_unit: bool,
    pub is_clone_path: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Classification {
    Eligible { rule: &'static str },
    Protected { reason: &'static str },
}

#[derive(Debug, PartialEq, Eq)]
pub enum State {
    Merged,
    Failed,
    Abandoned,
    Live,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Worktree {
    pub path: String,
    pub head: String,
    pub branch: Option<String>,
    /// The first porcelain entry remains the main worktree after sorting.
    pub is_main: bool,
    pub protection: Option<&'static str>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Repository {
    pub repo: String,
    pub porcelain: String,
    pub facts: BTreeMap<String, Facts>,
}

fn unquote(path: &str) -> Result<String, String> {
    if !path.starts_with('"') {
        return Ok(path.to_owned());
    }
    let path = path.strip_prefix('"').and_then(|s| s.strip_suffix('"'));
    let mut bytes = path.ok_or("unterminated path")?.bytes();
    let mut out = Vec::new();
    while let Some(b) = bytes.next() {
        out.push(if b != b'\\' {
            b
        } else {
            match bytes.next().ok_or("incomplete escape")? {
                b'a' => 7,
                b'b' => 8,
                b't' => 9,
                b'n' => 10,
                b'v' => 11,
                b'f' => 12,
                b'r' => 13,
                b'\\' => b'\\',
                b'"' => b'"',
                first @ b'0'..=b'3' => {
                    let octal = [
                        first,
                        bytes.next().ok_or("octal")?,
                        bytes.next().ok_or("octal")?,
                    ];
                    u8::from_str_radix(std::str::from_utf8(&octal).map_err(|_| "octal")?, 8)
                        .map_err(|_| "invalid octal escape")?
                }
                _ => return Err("invalid path escape".into()),
            }
        });
    }
    String::from_utf8(out).map_err(|_| "non-UTF-8 path".into())
}

pub fn parse_porcelain(text: &str) -> Result<Vec<Worktree>, String> {
    let mut trees = Vec::new();
    for block in text.split("\n\n").filter(|b| !b.trim().is_empty()) {
        let mut lines = block.lines();
        let first = lines.next().and_then(|s| s.strip_prefix("worktree "));
        let path = unquote(first.ok_or("expected worktree path")?)?;
        if path.is_empty() || path.contains('\0') || trees.iter().any(|w: &Worktree| w.path == path)
        {
            return Err("empty, NUL or duplicate path".into());
        }
        let mut w = Worktree {
            path,
            is_main: trees.is_empty(),
            ..Worktree::default()
        };
        let (mut bare, mut detached) = (false, false);
        let mut seen = BTreeSet::new();
        for line in lines {
            let (key, value) = line.split_once(' ').unwrap_or((line, ""));
            if !seen.insert(key) {
                return Err("duplicate porcelain field".into());
            }
            match key {
                "HEAD" if !value.is_empty() => w.head = value.into(),
                "branch" if value.starts_with("refs/heads/") => w.branch = Some(value.into()),
                "detached" if value.is_empty() => detached = true,
                "bare" if value.is_empty() => bare = true,
                "locked" => w.protection = Some("locked"),
                "prunable" => {
                    w.protection.get_or_insert("prunable");
                }
                _ => return Err("unknown or invalid porcelain field".into()),
            }
        }
        if bare {
            w.protection = Some("bare");
        }
        if (!bare && (w.head.is_empty() || (w.branch.is_some() == detached)))
            || (bare && (!w.head.is_empty() || w.branch.is_some() || detached))
        {
            return Err("incomplete or contradictory worktree".into());
        }
        trees.push(w);
    }
    if trees.is_empty() {
        return Err("empty porcelain inventory".into());
    }
    Ok(trees)
}

fn live_protection(w: &Worktree, f: &Facts, inventory: &[Worktree]) -> Option<&'static str> {
    let same_branch = |other: &Worktree| other.path != w.path && other.branch == w.branch;
    let duplicate = w.branch.is_some() && inventory.iter().any(same_branch);
    [
        (f.is_clone_path, "clone-path"),
        (f.in_use_by_process, "process-in-use"),
        (f.in_use_by_unit, "unit-in-use"),
        (f.has_claim, "claim"),
        (f.live_run, "live-run"),
        (f.open_pr, "open-pr"),
        (duplicate, "same-branch-worktree"),
    ]
    .into_iter()
    .find_map(|(p, r)| p.then_some(r))
    .or(w.protection)
    .or_else(|| w.is_main.then_some("main-worktree"))
}

/// Lifecycle labels are descriptive, not permission to reap an unmerged branch.
pub fn state(w: &Worktree, f: &Facts, inventory: &[Worktree]) -> State {
    if live_protection(w, f, inventory).is_some() {
        State::Live
    } else if f.merged_into_main {
        State::Merged
    } else if f.failed {
        State::Failed
    } else if f.idle_hours >= 72 {
        State::Abandoned
    } else {
        State::Live
    }
}

/// Facts and the complete inventory belong to one repository; no I/O or clock reads.
pub fn classify(w: &Worktree, f: &Facts, inventory: &[Worktree]) -> Classification {
    let reason = live_protection(w, f, inventory)
        .or_else(|| (!f.merged_into_main).then_some("unmerged-commits"))
        .or_else(|| (!f.pushed).then_some("unpushed-commits"));
    if let Some(reason) = reason {
        return Classification::Protected { reason };
    }
    let (hours, rule, reason) = if !f.clean {
        (168, "dirty-7d-needs-backup", "idle-below-7d")
    } else {
        (24, "merged-clean-24h", "idle-below-24h")
    };
    if f.idle_hours >= hours {
        Classification::Eligible { rule }
    } else {
        Classification::Protected { reason }
    }
}

fn tsv(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('\t', "\\t")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
}

/// Returns all rows, including protected paths. Nothing is written, even on errors.
pub fn report(repos: &[Repository]) -> Result<String, String> {
    let mut output = String::from("repo\tpath\tbranch\tstate\tstatus\treason\tneeds_backup\n");
    let mut repos: Vec<_> = repos.iter().collect();
    repos.sort_by_key(|r| &r.repo);
    let mut seen = BTreeSet::new();
    let mut seen_paths = BTreeSet::new();
    for repo in repos {
        if !seen.insert(&repo.repo) {
            return Err("duplicate repository".into());
        }
        let mut trees = parse_porcelain(&repo.porcelain)?;
        let paths: BTreeSet<_> = trees.iter().map(|w| &w.path).collect();
        if repo.facts.keys().any(|path| !paths.contains(path)) {
            return Err("facts path absent from repository inventory".into());
        }
        for path in paths {
            if !seen_paths.insert(path.clone()) {
                return Err("duplicate worktree path across repositories".into());
            }
        }
        trees.sort_by(|a, b| a.path.cmp(&b.path));
        for w in &trees {
            let state = match repo.facts.get(&w.path).map(|f| state(w, f, &trees)) {
                Some(State::Merged) => "merged",
                Some(State::Failed) => "failed",
                Some(State::Abandoned) => "abandoned",
                Some(State::Live) | None => "live",
            };
            let missing = Classification::Protected {
                reason: "missing-facts",
            };
            let verdict = repo
                .facts
                .get(&w.path)
                .map(|f| classify(w, f, &trees))
                .unwrap_or(missing);
            let (status, reason, backup) = match verdict {
                Classification::Eligible { rule } => {
                    ("eligible", rule, rule == "dirty-7d-needs-backup")
                }
                Classification::Protected { reason } => ("protected", reason, false),
            };
            let branch = w.branch.as_deref().unwrap_or("-");
            output += &format!(
                "{}\t{}\t{}\t{state}\t{status}\t{reason}\t{backup}\n",
                tsv(&repo.repo),
                tsv(&w.path),
                tsv(branch)
            );
        }
    }
    Ok(output)
}

pub mod orphans;
pub mod retention;
