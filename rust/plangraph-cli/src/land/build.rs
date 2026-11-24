//! The git-backed train builder and the verifier that repeats it (P3, P4, P5, P7, P9).
use super::delta::{Delta, DeltaMerge};
use super::git::{self, Checkout, Merged, POLICY_VERSION};
use super::resolve::Adopted;
use super::train::*;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

pub struct BuildInput<'a> {
    pub repo: &'a Path,
    pub build: String,
    pub base: String,
    /// The selection snapshot: READY parts in selection order.
    pub parts: Vec<PartIn>,
    /// Merge attributes from outside the tree (recorded in the manifest).
    pub attributes: String,
    /// Most parts one train holds; `None` while unbounded trains are the rule.
    pub cap: Option<usize>,
    /// Adopted resolutions of this build (from the keeper's log); a conflicting part with one is merged by it.
    pub resolutions: Vec<Adopted>,
    /// Path prefixes a resolution may not touch (gate paths, auth-door paths); `.gitattributes` is always one.
    pub protected: Vec<String>,
}

/// `land_build_result`: bound to the snapshot it was built from; merged and left out are exactly the snapshot.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct BuildResult {
    pub build: String,
    pub snapshot: Vec<PartIn>,
    pub merged: Vec<u64>,
    pub left_out: Vec<LeftOut>,
    pub cap: Option<usize>,
}

pub struct Outcome {
    /// `None` when nothing merged: there is no train, only the result.
    pub manifest: Option<Manifest>,
    pub result: BuildResult,
}

/// Holds the recorded attributes in a file for the duration of one operation.
struct Attrs {
    _dir: tempfile::TempDir,
    path: PathBuf,
}

impl Attrs {
    fn new(text: &str) -> Result<Attrs, String> {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let path = dir.path().join("attributes");
        std::fs::write(&path, text).map_err(|e| e.to_string())?;
        Ok(Attrs { _dir: dir, path })
    }
}

fn base_date(repo: &Path, base: &str) -> Result<String, String> {
    git::out(repo, &["show", "-s", "--format=%cI", base])
}

pub(super) fn part_base<'a>(repo: &Path, base: &'a str, parent: Option<&'a Parent>) -> &'a str {
    // An already-landed stack parent is covered by the current train base.
    match parent {
        Some(parent) if !in_base(repo, base, &parent.head) => &parent.head,
        _ => base,
    }
}

fn own_commits(repo: &Path, base: &str, part: &PartIn) -> Result<Vec<String>, String> {
    let from = part_base(repo, base, part.parent.as_ref());
    git::commits(repo, &format!("{from}..{}", part.head))
}

/// Report source paths found in more than one included part's own diff.
pub(super) fn shared_part_paths(repo: &Path, manifest: &Manifest) -> Result<Vec<String>, String> {
    let mut owners: BTreeMap<Vec<u8>, Vec<u64>> = BTreeMap::new();
    for part in &manifest.parts {
        let from = part_base(repo, &manifest.base, part.parent.as_ref());
        let paths = git::bytes(
            repo,
            &[
                "diff",
                "--merge-base",
                "--no-ext-diff",
                "--no-renames",
                "--name-only",
                "-z",
                from,
                &part.head,
                "--",
            ],
        )?;
        for path in paths
            .split(|&byte| byte == 0)
            .filter(|path| !path.is_empty())
        {
            if crate::batch_size::part_source_path(&String::from_utf8_lossy(path)) {
                owners.entry(path.to_vec()).or_default().push(part.pr);
            }
        }
    }
    Ok(owners
        .into_iter()
        .filter(|(_, parts)| parts.len() > 1)
        .map(|(path, parts)| {
            let ids: Vec<_> = parts.into_iter().map(|pr| format!("#{pr}")).collect();
            format!(
                "warning: train parts {} share {:?}",
                ids.join(", "),
                String::from_utf8_lossy(&path)
            )
        })
        .collect())
}

fn in_base(repo: &Path, base: &str, sha: &str) -> bool {
    git::is_ancestor(repo, sha, base).unwrap_or(false)
}

/// Builds the train on a detached checkout and returns manifest and result. The caller pushes nothing here.
pub fn build(input: &BuildInput) -> Result<Outcome, String> {
    for part in &input.parts {
        part.check_receipt()?;
    }
    let repo = input.repo;
    let attrs = Attrs::new(&input.attributes)?;
    let date = base_date(repo, &input.base)?;
    let checkout = Checkout::new(repo, &input.base)?;
    let ordered = stack_order(&input.parts);
    let mut merged_heads: BTreeMap<u64, String> = BTreeMap::new();
    let mut parts: Vec<Part> = Vec::new();
    let mut merges: Vec<String> = Vec::new();
    let mut left_out: Vec<LeftOut> = Vec::new();
    let mut resolution: Vec<Resolution> = Vec::new();
    let mut prefix = input.base.clone();
    let base_has = |sha: &str| in_base(repo, &input.base, sha);
    for (index, part) in ordered.iter().enumerate() {
        if !includable(part, &merged_heads, &base_has) {
            left_out.push(LeftOut {
                pr: part.pr,
                head: part.head.clone(),
                reason: Reason::Descendant,
                prefix: String::new(),
            });
            continue;
        }
        if input.cap.is_some_and(|cap| parts.len() >= cap) {
            left_out.push(LeftOut {
                pr: part.pr,
                head: part.head.clone(),
                reason: Reason::Cap,
                prefix: String::new(),
            });
            continue;
        }
        if let Some(q) = &part.parent {
            if !git::is_ancestor(repo, &q.head, &part.head)? {
                return Err(format!(
                    "part #{} does not contain its parent head",
                    part.pr
                ));
            }
        }
        let own = own_commits(repo, &input.base, part)?;
        let message = format!(
            "Merge part #{} ({})",
            part.pr,
            &part.head[..12.min(part.head.len())]
        );
        match git::merge(
            checkout.path(),
            Some(&attrs.path),
            &part.head,
            &message,
            &date,
        )? {
            Merged::Clean { commit, .. } => {
                merged_heads.insert(part.pr, part.head.clone());
                merges.push(commit.clone());
                prefix = commit;
                parts.push(Part {
                    pr: part.pr,
                    head: part.head.clone(),
                    parent: part.parent.clone(),
                    own: Own::of(&own),
                    receipt: part.receipt.clone(),
                });
            }
            Merged::Conflict { .. } => {
                let adopted = input.resolutions.iter().find(|a| {
                    // Bound to this build and this part's position in its snapshot order.
                    a.build == input.build
                        && a.position as usize == index + 1
                        && a.part == part.pr
                        && a.head == part.head
                        && a.onto == prefix
                });
                if let Some(a) =
                    adopted.filter(|a| check_resolution(repo, a, &input.protected).is_ok())
                {
                    git::out(checkout.path(), &["checkout", "-q", "--detach", &a.commit])?;
                    merged_heads.insert(part.pr, part.head.clone());
                    merges.push(a.commit.clone());
                    resolution.push(Resolution {
                        part: part.pr,
                        submission: a.submission.clone(),
                        commit: a.commit.clone(),
                    });
                    prefix = a.commit.clone();
                    parts.push(Part {
                        pr: part.pr,
                        head: part.head.clone(),
                        parent: part.parent.clone(),
                        own: Own::of(&own),
                        receipt: part.receipt.clone(),
                    });
                    continue;
                }
                left_out.push(LeftOut {
                    pr: part.pr,
                    head: part.head.clone(),
                    reason: Reason::Conflict,
                    prefix: prefix.clone(),
                });
            }
        }
    }
    let result = BuildResult {
        build: input.build.clone(),
        snapshot: input.parts.clone(),
        merged: parts.iter().map(|p| p.pr).collect(),
        left_out: left_out.clone(),
        cap: input.cap,
    };
    if parts.is_empty() {
        return Ok(Outcome {
            manifest: None,
            result,
        });
    }
    // Keep the train's objects in the repository under a hold ref until the branch is published.
    checkout.adopt(&prefix, &format!("refs/land/hold/{}", input.build))?;
    let ids: BTreeSet<u64> = parts.iter().map(|p| p.pr).collect();
    let graph = parts
        .iter()
        .filter_map(|p| {
            p.parent
                .as_ref()
                .filter(|q| ids.contains(&q.pr))
                .map(|q| (p.pr, q.pr))
        })
        .collect();
    let manifest = Manifest {
        build: input.build.clone(),
        policy: Policy {
            version: POLICY_VERSION.into(),
            git: git::git_version()?,
            attributes: input.attributes.clone(),
        },
        base: input.base.clone(),
        parts,
        graph,
        left_out,
        merges,
        resolution,
        train_sha: prefix,
    };
    Ok(Outcome {
        manifest: Some(manifest),
        result,
    })
}

/// Publishes the train as the branch `branch` (a ref under the builder's namespace).
pub fn publish(repo: &Path, branch: &str, manifest: &Manifest) -> Result<(), String> {
    if super::push::is_train_branch(branch) {
        super::push::write_manifest(repo, branch, manifest)?;
    }
    git::out(
        repo,
        &[
            "update-ref",
            &format!("refs/heads/{branch}"),
            &manifest.train_sha,
        ],
    )
    .map(|_| ())
}

/// The gate's proof that the train branch is exactly the manifest: closure, own commits, the whole
/// registered heads and dependencies, the attributed first-parent chain, a clean replay of every merge with the recorded
/// inputs, and the exact set of introduced commits.
pub fn verify_manifest(repo: &Path, m: &Manifest, selection: &[PartIn]) -> Result<(), String> {
    verify_manifest_with(repo, m, selection, &[], &[]).map(|_| ())
}

/// As `verify_manifest`, with the adopted resolutions of the freeze's build: a part merged by a resolution
/// must match an adopted result, pass both steps of P8, and would otherwise have conflicted on replay.
pub fn verify_manifest_with(
    repo: &Path,
    m: &Manifest,
    selection: &[PartIn],
    adopted: &[Adopted],
    protected: &[String],
) -> Result<Delta, String> {
    for part in selection {
        part.check_receipt()?;
    }
    if m.policy.version != POLICY_VERSION {
        return Err(format!(
            "policy version {} is not {POLICY_VERSION}",
            m.policy.version
        ));
    }
    if m.policy.git != git::git_version()? {
        return Err("git_version_mismatch: the replay must use the recorded Git version".into());
    }
    if m.resolution
        .iter()
        .any(|r| !m.parts.iter().any(|p| p.pr == r.part))
    {
        return Err("manifest: a resolution names a part that is not in the train".into());
    }
    if m.merges.len() != m.parts.len() {
        return Err("manifest: one merge per part".into());
    }
    let mut seen: BTreeMap<u64, String> = BTreeMap::new();
    let mut expected: BTreeSet<String> = m.merges.iter().cloned().collect();
    for part in &m.parts {
        let p_in = PartIn {
            pr: part.pr,
            head: part.head.clone(),
            parent: part.parent.clone(),
            receipt: part.receipt.clone(),
        };
        // The verifier recomputes the selection itself; the manifest cannot declare another head or parent.
        if !selection.contains(&p_in) {
            return Err(format!(
                "registered: part #{} differs from the registered head, dependencies or receipt",
                part.pr
            ));
        }
        if !includable(&p_in, &seen, &|s| in_base(repo, &m.base, s)) {
            return Err(format!(
                "closure: part #{} is in the train without its parent",
                part.pr
            ));
        }
        if let Some(q) = &part.parent {
            if !git::is_ancestor(repo, &q.head, &part.head)? {
                return Err(format!(
                    "closure: #{} does not contain its parent head",
                    part.pr
                ));
            }
        }
        let own = own_commits(repo, &m.base, &p_in)?;
        if Own::of(&own) != part.own {
            return Err(format!(
                "own commits of #{} differ from the manifest",
                part.pr
            ));
        }
        expected.extend(own);
        seen.insert(part.pr, part.head.clone());
    }
    use super::chain::Step;
    let heads: Vec<&str> = m.parts.iter().map(|part| part.head.as_str()).collect();
    // A remerge of main needs main; without the ref a train that carries one is refused.
    let main = git::out(repo, &["rev-parse", "--verify", super::chain::MAIN_REF]).ok();
    let chain = super::chain::validate(repo, &m.base, &m.train_sha, &heads, main.as_deref())?;
    let attrs = Attrs::new(&m.policy.attributes)?;
    let date = base_date(repo, &m.base)?;
    let mut replayed = Vec::with_capacity(m.parts.len());
    let mut resolutions = Vec::new();
    let mut remerges = Vec::new();
    for step in chain {
        let (commit, prev) = match step {
            Step::Merge { commit, parent } => (commit, parent),
            Step::Remerge {
                commit,
                parent,
                main,
            } => {
                // A remerge of main is clean by construction: a conflict or an extra edit is
                // a hand-made change that has no attribution and no review path.
                let check = Checkout::new(repo, &parent)?;
                let merge = git::merge(check.path(), Some(&attrs.path), &main, "replay", &date)?;
                let actual = git::out(repo, &["rev-parse", &format!("{commit}^{{tree}}")])?;
                match merge {
                    Merged::Clean { tree, .. } if tree == actual => {
                        expected.extend(git::commits(repo, &format!("{parent}..{main}"))?);
                        expected.insert(commit);
                        remerges.push(DeltaMerge::new([parent, main], actual, tree, None)?);
                    }
                    Merged::Clean { .. } => {
                        return Err(format!(
                            "replay: remerge of main {} has an extra or missing edit",
                            &main[..12]
                        ))
                    }
                    Merged::Conflict { .. } => {
                        return Err(format!(
                            "replay: remerge of main {} conflicts; rebuild the train",
                            &main[..12]
                        ))
                    }
                }
                continue;
            }
            Step::Resolution {
                commit,
                parent,
                writer,
                harness,
            } => {
                let patch = git::replay_diff(repo, Some(&attrs.path), &parent, &commit)?;
                expected.insert(commit.clone());
                resolutions.push(super::delta::DeltaResolution::new(
                    commit, parent, writer, harness, patch,
                )?);
                continue;
            }
        };
        let i = replayed.len();
        let part = &m.parts[i];
        if commit != m.merges[i] {
            return Err(format!(
                "chain: commit {commit} differs from the manifest merge of #{}",
                part.pr
            ));
        }
        let check = Checkout::new(repo, &prev)?;
        let resolved = m.resolution.iter().find(|r| r.part == part.pr);
        let merge = git::merge(check.path(), Some(&attrs.path), &part.head, "replay", &date)?;
        let actual = git::out(repo, &["rev-parse", &format!("{commit}^{{tree}}")])?;
        let (tree, remerge_diff) = match merge {
            Merged::Clean { tree, .. } => (tree, None),
            Merged::Conflict { tree } if resolved.is_some() => {
                let patch = git::replay_diff(check.path(), Some(&attrs.path), &tree, &actual)?;
                (tree, Some(patch))
            }
            Merged::Conflict { .. } => {
                return Err(format!("replay: merge of #{} conflicts", part.pr))
            }
        };
        if let Some(r) = resolved {
            let position = stack_order(selection)
                .iter()
                .position(|p| p.pr == part.pr)
                .map_or(0, |i| i as u32 + 1);
            let ok = adopted.iter().find(|a| {
                a.build == m.build
                    && a.position == position
                    && a.part == part.pr
                    && a.submission == r.submission
                    && a.commit == r.commit
                    && a.commit == m.merges[i]
            });
            let a = ok.ok_or_else(|| {
                format!(
                    "resolution: #{} has no adopted result of this build",
                    part.pr
                )
            })?;
            if a.onto != prev || a.head != part.head {
                return Err(format!(
                    "resolution: #{} was adopted for another prefix or head",
                    part.pr
                ));
            }
            check_resolution(repo, a, protected)
                .map_err(|e| format!("resolution: #{}: {e}", part.pr))?;
            if remerge_diff.is_none() {
                return Err(format!(
                    "resolution: #{} merges cleanly, no resolution is needed",
                    part.pr
                ));
            }
        } else if tree != actual {
            return Err(format!(
                "replay: merge of #{} has an extra or missing edit",
                part.pr
            ));
        }
        replayed.push(DeltaMerge::new(
            [prev, part.head.clone()],
            actual,
            tree,
            remerge_diff,
        )?);
    }
    let introduced: BTreeSet<String> = git::commits(repo, &format!("{}..{}", m.base, m.train_sha))?
        .into_iter()
        .collect();
    if introduced != expected {
        let extra: Vec<&String> = introduced.difference(&expected).collect();
        return Err(format!(
            "introduced commits differ from the manifest: {} unexpected",
            extra.len()
        ));
    }
    Ok(Delta {
        base: m.base.clone(),
        train_sha: m.train_sha.clone(),
        policy: m.policy.clone(),
        merges: replayed,
        remerges,
        resolutions,
    })
}

/// The gate's check of a build result against its snapshot and, when there is a train, its manifest.
pub fn verify_result(
    repo: &Path,
    base: &str,
    r: &BuildResult,
    manifest: Option<&Manifest>,
) -> Result<(), String> {
    let snap: BTreeSet<u64> = r.snapshot.iter().map(|p| p.pr).collect();
    let mut accounted: Vec<u64> = r
        .merged
        .iter()
        .copied()
        .chain(r.left_out.iter().map(|l| l.pr))
        .collect();
    accounted.sort_unstable();
    let mut want: Vec<u64> = snap.iter().copied().collect();
    want.sort_unstable();
    if accounted != want || snap.len() != r.snapshot.len() {
        return Err("result omits or repeats a part of its snapshot".into());
    }
    let merged_in_manifest: Vec<u64> = manifest
        .map(|m| m.parts.iter().map(|p| p.pr).collect())
        .unwrap_or_default();
    if merged_in_manifest != r.merged {
        return Err("merged parts differ from the manifest".into());
    }
    let ordered = stack_order(&r.snapshot);
    let attrs = Attrs::new(manifest.map_or("", |m| m.policy.attributes.as_str()))?;
    let date = base_date(repo, base)?;
    for l in &r.left_out {
        let part = r
            .snapshot
            .iter()
            .find(|p| p.pr == l.pr)
            .ok_or("left-out part is not in the snapshot")?;
        if part.head != l.head {
            return Err(format!(
                "left-out #{} names another head than the snapshot",
                l.pr
            ));
        }
        match l.reason {
            Reason::Conflict => {
                let before = ordered.iter().take_while(|p| p.pr != l.pr);
                let last_merged = before.filter(|p| r.merged.contains(&p.pr)).last();
                let derived = match (last_merged, manifest) {
                    (Some(p), Some(m)) => {
                        let i = m
                            .parts
                            .iter()
                            .position(|x| x.pr == p.pr)
                            .ok_or("merged part missing")?;
                        m.merges[i].clone()
                    }
                    _ => base.to_owned(),
                };
                if l.prefix != derived {
                    return Err(format!(
                        "wrong_prefix: #{} was tried on {} not on {derived}",
                        l.pr, l.prefix
                    ));
                }
                let check = Checkout::new(repo, &derived)?;
                if let Merged::Clean { .. } =
                    git::merge(check.path(), Some(&attrs.path), &part.head, "replay", &date)?
                {
                    return Err(format!(
                        "conflict_not_reproduced: #{} merges cleanly on {derived}",
                        l.pr
                    ));
                }
            }
            Reason::Descendant => {
                let q = part
                    .parent
                    .as_ref()
                    .ok_or(format!("descendant #{} has no parent", l.pr))?;
                let parent_merged = r.merged.contains(&q.pr);
                let landed = in_base(repo, base, &q.head);
                if parent_merged || landed {
                    return Err(format!(
                        "descendant_not_proven: parent of #{} is merged or landed",
                        l.pr
                    ));
                }
            }
            Reason::Cap => {
                if r.cap != Some(r.merged.len()) {
                    return Err(format!(
                        "cap_not_proven: #{} was left out below the cap",
                        l.pr
                    ));
                }
            }
        }
    }
    Ok(())
}

/// The two deterministic steps of P8 on the pinned object: its two parents are exactly the requested prefix
/// commit and the part head, and its change against the prefix touches no protected path.
pub fn check_resolution(repo: &Path, a: &Adopted, protected: &[String]) -> Result<(), String> {
    let parents = git::out(repo, &["rev-list", "--parents", "-n", "1", &a.commit])?;
    let row: Vec<&str> = parents.split(' ').collect();
    if row.len() != 3 || row[1] != a.onto || row[2] != a.head {
        return Err(
            "wrong parents: a resolution merges exactly the prefix and the part head".into(),
        );
    }
    let changed = git::out(repo, &["diff", "--name-only", &a.onto, &a.commit])?;
    for path in changed.lines() {
        let gitattributes = path == ".gitattributes" || path.ends_with("/.gitattributes");
        if gitattributes || protected.iter().any(|p| path.starts_with(p.as_str())) {
            return Err(format!("protected path {path} in a resolution"));
        }
    }
    Ok(())
}
