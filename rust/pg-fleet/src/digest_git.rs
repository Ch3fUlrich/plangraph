use crate::digest::{Runner, State};
use crate::digest_text::*;
use std::path::{Path, PathBuf};
pub fn git(runner: &dyn Runner, tree: &Path, args: &[&str]) -> (i32, String) {
    let argv: Vec<_> = [
        "--no-optional-locks".into(),
        "-C".into(),
        tree.to_string_lossy().into_owned(),
    ]
    .into_iter()
    .chain(args.iter().map(|s| s.to_string()))
    .collect();
    runner
        .run("git", &argv, None)
        .map_or((-1, String::new()), |o| (o.code, o.stdout))
}
pub fn trees(s: &State, runner: &dyn Runner) -> Vec<PathBuf> {
    let mut dirs = vec![s.role_cwd.clone()];
    for line in &s.drill {
        // Paths are extracted as tokens; reject a path embedded in another
        // word. Native Windows drive paths are accepted as well.
        for c in crate::handoff::re(r"(?:~?/|[A-Za-z]:[\\/])[\w.~+@%/\\-]+").find_iter(line) {
            if c.start() > 0
                && line[..c.start()]
                    .chars()
                    .next_back()
                    .is_some_and(|ch| ch.is_alphanumeric() || "./~-".contains(ch))
            {
                continue;
            }
            let p = crate::paths::expand(Path::new(c.as_str().trim_end_matches('.')));
            if p.exists() {
                dirs.push(if p.is_dir() {
                    p
                } else {
                    p.parent().unwrap().into()
                });
            }
        }
    }
    let mut trees = vec![];
    let mut seen = std::collections::HashSet::new();
    for d in dirs.into_iter().take(16) {
        if !seen.insert(d.clone()) {
            continue;
        }
        let (rc, top) = git(runner, &d, &["rev-parse", "--show-toplevel"]);
        if rc == 0 && !top.trim().is_empty() {
            let p = crate::paths::resolve(Path::new(top.trim()))
                .unwrap_or_else(|_| PathBuf::from(top.trim()));
            if !trees.contains(&p) {
                trees.push(p);
            }
        }
    }
    trees
}
pub fn section(s: &State, runner: &dyn Runner) -> Vec<String> {
    let trees = trees(s, runner);
    if trees.is_empty() {
        return vec![
            "== Git".into(),
            format!(
                "note: no git tree at {} or in the card's Drill-down",
                s.role_cwd.display()
            ),
        ];
    }
    let mut out = vec![format!(
        "== Git {} tree(s), {}",
        trees.len(),
        s.card_ts
            .map_or("no card stamp: last 3 commits".into(), |t| format!(
                "commits since the card stamp {}",
                stamp(t)
            ))
    )];
    for tree in trees.iter().take(3) {
        let (rc, mut branch) = git(runner, tree, &["symbolic-ref", "-q", "--short", "HEAD"]);
        if rc != 0 {
            let (rc, sha) = git(runner, tree, &["rev-parse", "--short", "HEAD"]);
            branch = if rc == 0 {
                format!("detached@{}", sha.trim())
            } else {
                "no commits".into()
            };
        }
        let (rc, ab) = git(
            runner,
            tree,
            &["rev-list", "--left-right", "--count", "HEAD...origin/main"],
        );
        let sync = if rc == 0 {
            ab.trim()
                .split_once('\t')
                .map_or("no origin/main".into(), |(a, b)| {
                    format!("ahead {a} behind {b}")
                })
        } else {
            "no origin/main".into()
        };
        let (rc, st) = git(runner, tree, &["status", "--porcelain"]);
        let dirty = if rc == 0 {
            st.lines()
                .filter(|s| !s.trim().is_empty())
                .count()
                .to_string()
        } else {
            "?".into()
        };
        out.push(cut(
            &format!(
                "  {}: {} | {sync} | dirty {dirty}",
                crate::paths::display(tree),
                branch.trim()
            ),
            160,
        ));
        let since = s
            .card_ts
            .map(|t| format!("--since={}", t.format("%Y-%m-%dT%H:%M:%S+00:00")));
        let args = if let Some(since) = &since {
            vec!["log", since, "--format=%h %s", "-n", "6"]
        } else {
            vec!["log", "--format=%h %s", "-n", "3"]
        };
        let (rc, log) = git(runner, tree, &args);
        let rows: Vec<_> = if rc == 0 {
            log.lines().collect()
        } else {
            vec![]
        };
        out.extend(
            rows.iter()
                .take(if since.is_some() { 5 } else { 3 })
                .map(|r| cut(&format!("    {r}"), 160)),
        );
        if let Some(since) = since {
            if rows.len() > 5 {
                let (rc, count) = git(runner, tree, &["rev-list", "--count", &since, "HEAD"]);
                let more = if rc == 0 {
                    count
                        .trim()
                        .parse::<usize>()
                        .ok()
                        .map(|n| n.saturating_sub(5))
                } else {
                    None
                };
                out.push(
                    more.filter(|n| *n > 0)
                        .map_or("    +more commits".into(), |n| {
                            format!("    +{n} more commits")
                        }),
                );
            }
            if rows.is_empty() {
                out.push(format!(
                    "    no commits since {}",
                    s.card_ts.unwrap().format("%H:%MZ")
                ));
            }
        }
    }
    if trees.len() > 3 {
        out.push(format!("  +{} more trees", trees.len() - 3));
    }
    out
}
