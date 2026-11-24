//! Binding of a deploy plan to the approved train and the landed tree, before anything is installed.
use super::deploy::{DeployPlan, Target};
use super::deploy_derive::{families_between, part_revs, touched_in};
use super::deploy_post::{named_targets, Gate};
use super::git;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// The train the protected GO evidence approved (its `head` is the train and its `base` the train's base).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Approved {
    pub train_sha: String,
    pub base: String,
}

pub(super) fn wall_clock() -> i64 {
    chrono::Utc::now().timestamp_micros()
}

fn name_of(t: &Target) -> String {
    format!("{}@{}", t.family, t.host)
}

/// Every target must be something the approved train really ships: its family is in the net diff
/// `base..train`, a unit still exists in the landed tree, and each item key belongs to a commit that touches
/// the family (or, for a family an untagged commit touches, to the train).
fn check_in_tree(plan: &DeployPlan, repo: &Path, landed_head: &str) -> Result<(), String> {
    let net = families_between(repo, &plan.base, &plan.train_sha)?;
    // Items allowed per family, part by part: a commit without `Plan-Item` counts for the items of its own
    // part only, never for another part's.
    let mut allowed: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for revs in part_revs(repo, &plan.base, &plan.train_sha)? {
        let touched = touched_in(repo, &revs, &net)?;
        for (family, shipped) in &touched.by_family {
            let e = allowed.entry(family.clone()).or_default();
            e.extend(shipped.items.iter().cloned());
            if touched.untagged.contains(family) {
                e.extend(touched.items.iter().cloned());
            }
        }
    }
    for t in &plan.targets {
        if !net.contains_key(&t.family) {
            return Err(format!(
                "{} is not shipped by train {}",
                t.family, plan.train_sha
            ));
        }
        if !t.family.starts_with("bin:")
            && git::out(
                repo,
                &[
                    "cat-file",
                    "-t",
                    &format!("{landed_head}:deploy/systemd/{}", t.family),
                ],
            )
            .is_err()
        {
            return Err(format!(
                "{} is absent from the landed tree {landed_head}",
                t.family
            ));
        }
        let own = allowed.get(&t.family);
        if let Some(k) = t
            .items
            .iter()
            .find(|k| !own.is_some_and(|o| o.contains(*k)))
        {
            return Err(format!(
                "item {k} does not ship {} in train {}",
                name_of(t),
                plan.train_sha
            ));
        }
    }
    Ok(())
}

/// The plan, the approved train, the GO row and the landed head must belong together before anything is
/// installed: the plan is for the approved train and base, the GO row is the plan's own `deploy:` line, the
/// plan matches its digest, the train is merged into the landed head and the head is on main. Returns the
/// targets and the gate their first runs are verified against.
pub fn bind(
    plan: &DeployPlan,
    approved: &Approved,
    go_row: &str,
    repo: &Path,
    landed_head: &str,
    main_ref: &str,
    plan_digest: &str,
) -> Result<(Vec<Target>, Gate), String> {
    if plan.train_sha.is_empty() || plan.base.is_empty() {
        return Err("the deploy plan names no train or base (rerun deploy-plan --plan-out)".into());
    }
    if plan.train_sha != approved.train_sha || plan.base != approved.base {
        return Err(format!(
            "the plan is for train {} on {}, the approved GO is for {} on {}",
            plan.train_sha, plan.base, approved.train_sha, approved.base
        ));
    }
    if plan.digest() != plan_digest.trim() {
        return Err("the plan does not match the approved plan digest".into());
    }
    if go_row.trim() != plan.go_req() {
        return Err(format!(
            "GO row {go_row:?} is not this plan's {:?}",
            plan.go_req()
        ));
    }
    if !git::is_ancestor(repo, &plan.train_sha, landed_head)? {
        return Err(format!(
            "train {} is not merged into landed head {landed_head}",
            plan.train_sha
        ));
    }
    if !git::is_ancestor(repo, landed_head, main_ref)? {
        return Err(format!("landed head {landed_head} is not on {main_ref}"));
    }
    check_in_tree(plan, repo, landed_head)?;
    let fresh_after = 1_000_000
        * git::out(repo, &["show", "-s", "--format=%ct", landed_head])?
            .trim()
            .parse::<i64>()
            .map_err(|e| format!("commit time of {landed_head}: {e}"))?;
    let excluded: BTreeSet<(String, String)> = plan
        .blockers
        .iter()
        .filter_map(|b| Some((b.family.clone(), b.host.clone()?)))
        .collect();
    let gate = Gate {
        fresh_after,
        clock: wall_clock,
        excluded,
    };
    Ok((named_targets(plan, go_row)?, gate))
}
