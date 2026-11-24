//! First-parent train validation shared by the push gate and replay.
use super::git;
use std::{collections::BTreeSet, path::Path};

pub(super) enum Step {
    Merge {
        commit: String,
        parent: String,
    },
    Resolution {
        commit: String,
        parent: String,
        writer: String,
        harness: String,
    },
    /// The new `origin/main` merged into an open train (rule 3.63 (3)): a two-parent commit whose second
    /// parent is a first-parent commit of main. Replay recomputes it and records it for the remerge-diff review.
    Remerge {
        commit: String,
        parent: String,
        main: String,
    },
}

/// The ref the push gate and replay read as main.
pub(super) const MAIN_REF: &str = "refs/remotes/origin/main";

fn full_sha(sha: &str) -> bool {
    sha.len() == 40
        && sha
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn resolver_subject(subject: &str) -> bool {
    let Some((number, action)) = subject
        .strip_prefix("Train ")
        .and_then(|subject| subject.split_once(": "))
    else {
        return false;
    };
    !number.is_empty()
        && number.bytes().all(|b| b.is_ascii_digit())
        && ["resolve", "drop"].iter().any(|verb| {
            action
                .strip_prefix(verb)
                .is_some_and(|rest| rest.is_empty() || rest.starts_with(char::is_whitespace))
        })
}

fn resolver(repo: &Path, commit: &str) -> Result<(String, String), String> {
    let fields = git::out(
        repo,
        &[
            "show",
            "-s",
            "--format=%s%x00%(trailers:key=Writer,valueonly,separator=%x1f)%x00%(trailers:key=Harness,valueonly,separator=%x1f)",
            commit,
        ],
    )?;
    let valid = |value: &str| !value.trim().is_empty() && !value.contains('\x1f');
    let split: Vec<&str> = fields.split('\0').collect();
    let [subject, writer, harness] = split.as_slice() else {
        return Err(format!(
            "chain: cannot read resolver attribution for {commit}"
        ));
    };
    if !resolver_subject(subject) {
        return Err(format!(
            "chain: train commit {commit} is an extra commit without resolver attribution"
        ));
    }
    for (name, value) in [("Writer", writer), ("Harness", harness)] {
        if !valid(value) {
            return Err(format!(
                "chain: train commit {commit} is an extra commit without a valid {name} trailer"
            ));
        }
    }
    Ok((writer.trim().into(), harness.trim().into()))
}

#[cfg(test)]
#[test]
fn resolver_diagnostic_names_the_offending_commit_and_trailer() {
    use super::build_tests::{g, Fx};

    let fixture = Fx::new();
    let repo = fixture.path();
    for (subject, trailers, missing) in [
        ("Train 9: resolve fixture", "Writer: codex", "Harness"),
        ("Train 9: drop fixture", "Harness: codex", "Writer"),
    ] {
        let message = format!("{subject}\n\n{trailers}");
        g(repo, &["commit", "--allow-empty", "-m", &message]);
        let head = g(repo, &["rev-parse", "HEAD"]);
        assert_eq!(
            resolver(repo, &head).unwrap_err(),
            format!(
                "chain: train commit {head} is an extra commit without a valid {missing} trailer"
            )
        );
    }
}

pub(super) fn validate(
    repo: &Path,
    base: &str,
    tip: &str,
    heads: &[&str],
    main: Option<&str>,
) -> Result<Vec<Step>, String> {
    if heads.is_empty()
        || ![base, tip]
            .into_iter()
            .chain(heads.iter().copied())
            .all(full_sha)
    {
        return Err("chain: train needs full lowercase base, head and part SHAs".into());
    }
    let history = git::out(
        repo,
        &[
            "rev-list",
            "--first-parent",
            "--reverse",
            "--parents",
            &format!("{base}..{tip}"),
        ],
    )?;
    let mut previous = base;
    let mut index = 0;
    let mut steps = Vec::new();
    let mut main_line: Option<BTreeSet<String>> = None;
    for line in history.lines() {
        let row: Vec<&str> = line.split_whitespace().collect();
        let step = match row.as_slice() {
            [commit, parent, landed] if *parent == previous && !heads.contains(landed) => {
                // Only the remerge of main's own history is allowed; any other second parent could
                // bring in a commit that is neither a part nor landed.
                let Some(main) = main else {
                    return Err(format!(
                        "chain: train parent {} is outside its manifest",
                        &landed[..12]
                    ));
                };
                if main_line.is_none() {
                    let line = git::out(repo, &["rev-list", "--first-parent", main])?;
                    main_line = Some(line.lines().map(str::to_owned).collect());
                }
                if !main_line.as_ref().is_some_and(|l| l.contains(*landed))
                    || git::is_ancestor(repo, landed, previous)?
                {
                    return Err(format!(
                        "chain: train parent {} is outside its manifest and is not new first-parent history of main",
                        &landed[..12]
                    ));
                }
                Step::Remerge {
                    commit: (*commit).into(),
                    parent: (*parent).into(),
                    main: (*landed).into(),
                }
            }
            [commit, parent, part] if *parent == previous => {
                if !heads.contains(part) {
                    return Err(format!(
                        "chain: train parent {} is outside its manifest",
                        &part[..12]
                    ));
                }
                if heads.get(index) != Some(part) {
                    return Err(format!(
                        "chain: train parent {} is out of manifest order",
                        &part[..12]
                    ));
                }
                index += 1;
                Step::Merge {
                    commit: (*commit).into(),
                    parent: (*parent).into(),
                }
            }
            [commit, parent] if *parent == previous => {
                let (writer, harness) = resolver(repo, commit)?;
                Step::Resolution {
                    commit: (*commit).into(),
                    parent: (*parent).into(),
                    writer,
                    harness,
                }
            }
            _ => {
                return Err(format!(
                    "chain: train commit does not extend its first-parent predecessor: {line}"
                ))
            }
        };
        previous = row[0];
        steps.push(step);
    }
    if index != heads.len() || previous != tip {
        return Err(format!(
            "chain: train has {index} merges, its manifest has {} parts",
            heads.len()
        ));
    }
    Ok(steps)
}

/// The commit the train's first-parent chain leaves main at. The merge-base cannot serve: after a remerge
/// of the new main the train contains main, so its merge-base with main is main itself.
fn train_base(repo: &Path, tip: &str) -> Result<String, String> {
    let unavailable = |_| "train base unavailable (no origin/main first-parent base)".to_owned();
    let own = git::out(
        repo,
        &["rev-list", "--first-parent", tip, "--not", MAIN_REF],
    )
    .map_err(unavailable)?;
    let Some(last) = own.lines().last() else {
        return Ok(tip.to_owned());
    };
    git::out(repo, &["rev-parse", &format!("{last}^")]).map_err(unavailable)
}

pub(super) fn check_push(repo: &Path, tip: &str, manifest: &Path) -> Result<(), String> {
    let manifest = std::fs::read_to_string(manifest)
        .map_err(|err| format!("train manifest unavailable: {err}"))?;
    let heads: Vec<&str> = manifest
        .lines()
        .map(|line| line.split('#').next().unwrap_or("").trim())
        .filter(|line| !line.is_empty())
        .collect();
    if !full_sha(tip) {
        return Err("train head must be a full lowercase SHA".into());
    }
    let base = train_base(repo, tip)?;
    validate(repo, &base, tip, &heads, Some(MAIN_REF)).map(|_| ())
}
