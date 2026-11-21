use super::sources::{command, level, urgent_message, CAP};
use super::{Config, Event, Observation, Result};
use serde_json::{json, Value};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::process::Command;
use std::time::Instant;

fn open_regular(path: &Path) -> Result<File> {
    if !fs::symlink_metadata(path)
        .map_err(|_| "source file unavailable")?
        .is_file()
    {
        return Err("source is not a regular file".into());
    }
    File::open(path).map_err(|_| "source file unavailable".into())
}

pub(super) fn lines(
    config: &Config,
    source: &str,
    path: &Path,
    cursor: Value,
) -> Result<Observation> {
    let mut file = open_regular(path)?;
    let metadata = file.metadata().map_err(|_| "cannot inspect source")?;
    #[cfg(unix)]
    let identity = {
        use std::os::unix::fs::MetadataExt;
        format!("{}:{}", metadata.dev(), metadata.ino())
    };
    #[cfg(not(unix))]
    let identity = path.display().to_string();
    let offset = if cursor["file"] == identity
        && cursor["offset"]
            .as_u64()
            .is_some_and(|s| s <= metadata.len())
    {
        cursor["offset"].as_u64().unwrap()
    } else {
        0
    };
    file.seek(SeekFrom::Start(offset))
        .map_err(|_| "cannot seek source")?;
    let mut bytes = Vec::new();
    file.take(CAP)
        .read_to_end(&mut bytes)
        .map_err(|_| "cannot read source")?;
    let complete = bytes.iter().rposition(|&b| b == b'\n').map_or(0, |i| i + 1);
    if complete == 0 && bytes.len() as u64 == CAP {
        return Err("record exceeds source limit".into());
    }
    let text = std::str::from_utf8(&bytes[..complete]).map_err(|_| "invalid source text")?;
    let mut events = Vec::new();
    for line in text.lines().filter(|line| !line.is_empty()) {
        let data = if source == "transcript" {
            let row: Value = serde_json::from_str(line).map_err(|_| "invalid transcript row")?;
            if row["type"] != "queue-operation"
                || row["operation"] != "enqueue"
                || row
                    .get("sessionId")
                    .is_some_and(|id| id != &config.session_id)
            {
                continue;
            }
            row
        } else {
            json!(line)
        };
        let mut event = Event::new(source, "message", data);
        event.urgent = urgent_message(&event.data.to_string());
        event.untrusted = true;
        events.push(event);
    }
    Ok((
        json!({"file": identity, "offset":offset + complete as u64}),
        events,
        false,
    ))
}

pub(super) fn sha(value: &str) -> bool {
    value.len() == 40 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn git(config: &Config, args: &[&str], deadline: Instant) -> Result<String> {
    let mut cmd = Command::new("git");
    cmd.current_dir(&config.data_repo).args(args);
    command(cmd, deadline)
}

pub(super) fn decisions(config: &Config, cursor: Value, deadline: Instant) -> Result<Observation> {
    let since = cursor.as_str().unwrap_or(&config.decision_base);
    if !sha(since) {
        return Err("invalid decision bookmark".into());
    }
    let tip = git(config, &["rev-parse", "refs/remotes/origin/main"], deadline)?;
    let tip = tip.trim();
    git(
        config,
        &["merge-base", "--is-ancestor", since, tip],
        deadline,
    )?;
    let range = format!("{since}..{tip}");
    let path = "router/docs/decisions-log.md";
    let commits = git(
        config,
        &[
            "rev-list",
            "--reverse",
            "--first-parent",
            &range,
            "--",
            path,
        ],
        deadline,
    )?;
    let mut events = Vec::new();
    for commit in commits.lines().take(100) {
        let patch = git(
            config,
            &[
                "show",
                "--format=",
                "--first-parent",
                "--diff-merges=first-parent",
                "--unified=0",
                commit,
                "--",
                path,
            ],
            deadline,
        )?;
        for line in patch
            .lines()
            .filter_map(|line| line.strip_prefix('+'))
            .filter(|line| !line.starts_with("++"))
        {
            let words: Vec<_> = line
                .split(|ch: char| ch.is_whitespace() || "|,();`".contains(ch))
                .collect();
            if !words.iter().any(|word| {
                *word == config.role
                    || *word == config.principal
                    || config.item_keys.iter().any(|key| word == key)
            }) {
                continue;
            }
            let kind = if words.contains(&"GO") {
                "go"
            } else if words.iter().any(|word| word.eq_ignore_ascii_case("grant")) {
                "grant"
            } else {
                "decision"
            };
            let mut event = Event::new("decisions", kind, json!({"commit":commit,"line":line}));
            event.urgent = kind == "go" || kind == "grant";
            events.push(event);
        }
    }
    let next = commits
        .lines()
        .nth(100)
        .map_or(tip, |_| commits.lines().nth(99).unwrap());
    Ok((json!(next), events, false))
}

pub(super) fn quota(config: &Config, cursor: Value) -> Result<Observation> {
    let mut bytes = Vec::new();
    open_regular(&config.quota)?
        .take(CAP + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "cannot read quota status")?;
    if bytes.len() as u64 > CAP {
        return Err("quota source exceeds limit".into());
    }
    let current = serde_json::from_slice(&bytes).map_err(|_| "invalid quota status")?;
    Ok(level("quota", "levels", current, cursor))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn observes_scoped_go_from_data_commits_without_a_message() {
        let dir = tempfile::tempdir().unwrap();
        let mut config = super::super::fixture(dir.path());
        let git = |args: &[&str]| {
            let mut command = Command::new("git");
            command
                .current_dir(dir.path())
                .args(args)
                .env("GIT_AUTHOR_NAME", "Fixture")
                .env("GIT_COMMITTER_NAME", "Fixture")
                .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
                .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid");
            super::command(command, Instant::now() + std::time::Duration::from_secs(5)).unwrap()
        };
        git(&["init", "-q"]);
        let log = dir.path().join("router/docs/decisions-log.md");
        fs::create_dir_all(log.parent().unwrap()).unwrap();
        fs::write(&log, "decisions\n").unwrap();
        git(&["add", "router/docs/decisions-log.md"]);
        git(&["commit", "-qm", "initial"]);
        config.decision_base = git(&["rev-parse", "HEAD"]).trim().into();
        fs::write(
            &log,
            "decisions\n| D-1 | GO routing demo:A |\n| D-2 | GO another-role |\n",
        )
        .unwrap();
        git(&["commit", "-qam", "two decisions"]);
        git(&["update-ref", "refs/remotes/origin/main", "HEAD"]);
        let deadline = Instant::now() + std::time::Duration::from_secs(5);
        let (cursor, events, _) = decisions(&config, Value::Null, deadline).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].kind, "go");
        assert!(events[0].urgent);
        assert!(events[0].data["line"].as_str().unwrap().contains("D-1"));
        assert!(decisions(&config, cursor, deadline).unwrap().1.is_empty());
    }
}
