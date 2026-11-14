//! Attribute commits to the first mainline merge introducing them in one walk.
use crate::keeper_truth::{parse_commits, Commit};
use std::collections::{BTreeMap, BTreeSet};

pub fn history(log: &str) -> Result<Vec<Commit>, String> {
    let mut parents = BTreeMap::new();
    let mut commits = Vec::new();
    for record in log
        .split('\x1d')
        .map(|r| r.trim_matches('\n'))
        .filter(|r| !r.is_empty())
    {
        let (line, parent_list) = record.rsplit_once('\x1f').ok_or("invalid graph record")?;
        let commit = parse_commits(line)?.pop().ok_or("invalid graph commit")?;
        let parent_list: Vec<String> = parent_list.split_whitespace().map(str::to_owned).collect();
        if parent_list
            .iter()
            .any(|p| p.len() != 40 || !p.bytes().all(|b| b.is_ascii_hexdigit()))
        {
            return Err("invalid graph parent".into());
        }
        parents.insert(commit.sha.clone(), parent_list);
        commits.push(commit);
    }
    let mut chain = Vec::new();
    let mut next = commits.first().map(|c| c.sha.as_str());
    while let Some(sha) = next {
        if !parents.contains_key(sha) {
            break; // PR deltas intentionally omit ancestors on the default ref.
        }
        chain.push(sha.to_owned());
        next = parents.get(sha).and_then(|p| p.first()).map(String::as_str);
    }
    let by_sha: BTreeMap<_, _> = commits
        .iter()
        .enumerate()
        .map(|(i, c)| (c.sha.clone(), i))
        .collect();
    let mut visited = BTreeSet::new();
    for sha in chain.into_iter().rev() {
        let pr = pr_number(&commits[by_sha[&sha]].subject);
        let mut pending = vec![sha];
        while let Some(sha) = pending.pop() {
            if !visited.insert(sha.clone()) {
                continue;
            }
            if let Some(&i) = by_sha.get(&sha) {
                if let Some(pr) = pr {
                    commits[i].prs.push(pr);
                }
            }
            if let Some(ancestors) = parents.get(&sha) {
                pending.extend(ancestors.iter().cloned());
            }
        }
    }
    Ok(commits)
}

fn pr_number(subject: &str) -> Option<u64> {
    let rest = subject
        .strip_prefix("Merge PR #")
        .or_else(|| subject.strip_prefix("Merge pull request #"))
        .or_else(|| {
            subject
                .rsplit_once("(#")
                .filter(|_| subject.ends_with(')'))
                .map(|p| p.1)
        })?;
    rest.chars()
        .take_while(char::is_ascii_digit)
        .collect::<String>()
        .parse()
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn keeper_truth_open_pr_delta_can_end_at_excluded_default_parent() {
        let log = format!(
            "{}\x1f2026-11-14\x1fwork\x1fdemo:A\x1f{}\x1d",
            "a".repeat(40),
            "b".repeat(40)
        );
        let commits = history(&log).unwrap();
        assert_eq!(commits.len(), 1);
        assert_eq!(commits[0].keys, vec!["demo:A"]);
    }
    #[test]
    fn keeper_reconcile_introducing_pr_from_merge_graph() {
        let a = "a".repeat(40);
        let b = "b".repeat(40);
        let c = "c".repeat(40);
        let log = format!("{a}\x1f2026-11-14\x1fMerge PR #42\x1f\x1f{c} {b}\x1d\n{b}\x1f2026-11-13\x1fwork\x1fdemo:A\x1f{c}\x1d\n{c}\x1f2026-11-12\x1fbase\x1f\x1f\x1d");
        let commits = history(&log).unwrap();
        assert_eq!(commits[1].prs, vec![42]);
        assert!(commits[2].prs.is_empty());
    }
}
