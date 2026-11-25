//! Stale-lane alert: a writer status that says DONE while its head is not on the remote.
//!
//! plangraph wait lane-alert --status FILE [--remote origin] [--branch lane/NAME] [--grace SECS]
//! Exit 0 when the head is on the remote (or the writer is not done, or inside the grace), 1 with an
//! `ALERT` line otherwise, 2 on a usage or git error. The writer pushes once per round; a DONE
//! whose head never reached the remote after the grace (default 600 s) is nobody's to review.
use std::path::Path;
use std::process::Command;
use std::time::{Duration, SystemTime};

const DEFAULT_GRACE: Duration = Duration::from_secs(600);

#[derive(Debug, PartialEq)]
pub struct Alert {
    pub name: String,
    pub head: String,
    pub age_secs: u64,
}

/// The writer name and head of a finished status file, `None` while it is not `<NAME> DONE` + `HEAD:`.
pub fn finished(text: &str) -> Option<(String, String)> {
    let name = text
        .lines()
        .find_map(|l| l.strip_suffix(" DONE").filter(|n| !n.contains(' ')))?;
    let head = text
        .lines()
        .rev()
        .find_map(|l| l.strip_prefix("HEAD: "))?
        .trim();
    (head.len() == 40 && head.bytes().all(|b| b.is_ascii_hexdigit()))
        .then(|| (name.to_owned(), head.to_owned()))
}

/// The alert for a status of `age` whose head is (not) `on_remote`, once the grace has passed.
pub fn stale(
    text: &str,
    age: Duration,
    on_remote: impl FnOnce(&str) -> bool,
    grace: Duration,
) -> Option<Alert> {
    let (name, head) = finished(text)?;
    if age <= grace || on_remote(&head) {
        return None;
    }
    Some(Alert {
        name,
        head,
        age_secs: age.as_secs(),
    })
}

fn git(args: &[&str]) -> Result<(bool, String), String> {
    let out = Command::new("git")
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    Ok((
        out.status.success(),
        String::from_utf8_lossy(&out.stdout).trim().to_owned(),
    ))
}

/// Whether `head` is the tip of `branch` on `remote`, or an ancestor of that tip.
fn head_on_remote(remote: &str, branch: &str, head: &str) -> Result<bool, String> {
    let (ok, listing) = git(&["ls-remote", remote, &format!("refs/heads/{branch}")])?;
    if !ok {
        return Err("git ls-remote failed".into());
    }
    let Some(tip) = listing.split_whitespace().next() else {
        return Ok(false);
    };
    Ok(tip == head || git(&["merge-base", "--is-ancestor", head, tip])?.0)
}

pub fn run(args: &[String]) -> i32 {
    let (mut status, mut remote, mut branch, mut grace) =
        (None, "origin".to_owned(), None, DEFAULT_GRACE);
    let mut i = 0;
    while i < args.len() {
        let value = args.get(i + 1).cloned();
        match (args[i].as_str(), value) {
            ("--status", Some(v)) => status = Some(v),
            ("--remote", Some(v)) => remote = v,
            ("--branch", Some(v)) => branch = Some(v),
            ("--grace", Some(v)) => match v.parse() {
                Ok(secs) => grace = Duration::from_secs(secs),
                Err(_) => return usage(),
            },
            _ => return usage(),
        }
        i += 2;
    }
    let Some(status) = status else { return usage() };
    let text = std::fs::read_to_string(&status).unwrap_or_default();
    let age = std::fs::metadata(Path::new(&status))
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| SystemTime::now().duration_since(t).ok())
        .unwrap_or_default();
    let mut failure = None;
    let alert = stale(
        &text,
        age,
        |head| {
            let name = finished(&text).map(|(n, _)| n).unwrap_or_default();
            let branch = branch.clone().unwrap_or_else(|| format!("lane/{name}"));
            head_on_remote(&remote, &branch, head).unwrap_or_else(|e| {
                failure = Some(e);
                true
            })
        },
        grace,
    );
    if let Some(error) = failure {
        eprintln!("wait lane-alert: {error}");
        return 2;
    }
    match alert {
        Some(a) => {
            println!(
                "ALERT lane-head-not-on-remote name={} head={} age={}s",
                a.name, a.head, a.age_secs
            );
            1
        }
        None => {
            println!("OK lane-head");
            0
        }
    }
}

fn usage() -> i32 {
    eprintln!("usage: plangraph wait lane-alert --status FILE [--remote origin] [--branch lane/NAME] [--grace SECS]");
    2
}

#[cfg(test)]
mod tests {
    use super::*;

    const HEAD: &str = "0123456789abcdef0123456789abcdef01234567";
    fn status() -> String {
        format!("DONE plangraph:A abc\npr10-writer-flow DONE\nHEAD: {HEAD}\nREVIEW: none\nVERDICT: ACCEPT\n")
    }

    #[test]
    fn a_done_status_with_its_head() {
        assert_eq!(
            finished(&status()),
            Some(("pr10-writer-flow".into(), HEAD.into()))
        );
        assert_eq!(
            finished("DONE plangraph:A abc\n"),
            None,
            "item lines alone are not the end"
        );
        assert_eq!(finished("x DONE\nHEAD: short\n"), None);
    }

    #[test]
    fn done_without_a_remote_head_after_the_grace_raises_the_alert() {
        let alert = stale(
            &status(),
            Duration::from_secs(700),
            |_| false,
            Duration::from_secs(600),
        )
        .unwrap();
        assert_eq!(
            (alert.name.as_str(), alert.head.as_str(), alert.age_secs),
            ("pr10-writer-flow", HEAD, 700)
        );
    }

    #[test]
    fn a_pushed_head_a_young_status_or_an_unfinished_writer_raise_none() {
        let grace = Duration::from_secs(600);
        assert_eq!(
            stale(&status(), Duration::from_secs(700), |_| true, grace),
            None
        );
        assert_eq!(
            stale(&status(), Duration::from_secs(60), |_| false, grace),
            None
        );
        assert_eq!(
            stale(
                "DONE plangraph:A abc\n",
                Duration::from_secs(9000),
                |_| false,
                grace
            ),
            None
        );
    }
}
