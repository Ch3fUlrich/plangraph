//! Checklist generation and status-note admission for criterion-bearing Rust cards.
use crate::{
    errors::ToolError,
    launch::LaunchSpec,
    placement::{parameter, Args},
};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::Path,
    time::{Duration, SystemTime},
};

const CLASSES: [(&str, &[&str]); 4] = [
    (
        "Verify regression tests reproduce the fix and pass on this head",
        &["test", "coverage"],
    ),
    (
        "Verify documentation agrees with the implemented behavior",
        &["doc", "readme", "changelog"],
    ),
    (
        "Verify cross-language and caller contracts agree",
        &["contract", "python", "rust", "schema"],
    ),
    ("Verify other recent HIGH/MED findings do not recur", &[]),
];

fn finding_counts(directory: &Path) -> Result<[usize; 4], ToolError> {
    let mut counts = [0; 4];
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(counts),
        Err(_) => return Err(parameter("cannot read pre-review verdict directory")),
    };
    let now = SystemTime::now();
    for entry in entries {
        let path = entry
            .map_err(|_| parameter("cannot read verdict entry"))?
            .path();
        if !matches!(
            path.extension().and_then(|s| s.to_str()),
            Some("txt" | "md")
        ) {
            continue;
        }
        let meta =
            fs::symlink_metadata(&path).map_err(|_| parameter("cannot read verdict metadata"))?;
        if !meta.is_file() || meta.len() > 1024 * 1024 {
            continue;
        }
        let age = now.duration_since(
            meta.modified()
                .map_err(|_| parameter("cannot date verdict"))?,
        );
        if !age.is_ok_and(|age| age <= Duration::from_secs(30 * 86400)) {
            continue;
        }
        let text = fs::read_to_string(&path).map_err(|_| parameter("cannot read verdict text"))?;
        if !text.lines().any(|l| l.trim().starts_with("VERDICT: ")) {
            continue;
        }
        for line in text.lines() {
            let finding =
                line.trim_start_matches(|c: char| c.is_whitespace() || "#-*[(".contains(c));
            let severity = finding.split(|c: char| !c.is_alphanumeric()).next();
            if !matches!(severity, Some("HIGH" | "MED" | "P1" | "P2")) {
                continue;
            }
            let lower = line.to_lowercase();
            let mut found = false;
            for (i, (_, keywords)) in CLASSES[..3].iter().enumerate() {
                if keywords.iter().any(|k| lower.contains(k)) {
                    counts[i] += 1;
                    found = true;
                }
            }
            if !found {
                counts[3] += 1;
            }
        }
    }
    Ok(counts)
}
fn checklist(card: &serde_json::Value, cwd: &Path) -> Result<Vec<(String, String)>, ToolError> {
    let criteria = card["done_criteria"]
        .as_str()
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| parameter("done_criteria must be a nonempty string"))?;
    let directory = match card.get("review_dir") {
        Some(value) => {
            let directory = value
                .as_str()
                .filter(|s| !s.trim().is_empty())
                .ok_or_else(|| parameter("review_dir must be a nonempty path"))?;
            cwd.join(directory)
        }
        None => cwd.join("reviews"),
    };
    let counts = finding_counts(&directory)?;
    let mut ranked: Vec<_> = (0..CLASSES.len()).filter(|i| counts[*i] > 0).collect();
    ranked.sort_by_key(|i| (std::cmp::Reverse(counts[*i]), *i));
    let mut lines = vec![format!(
        "Verify every done criterion: {}",
        serde_json::json!(criteria)
    )];
    lines.extend(ranked.into_iter().take(3).map(|i| CLASSES[i].0.to_owned()));
    Ok(lines
        .into_iter()
        .map(|line| {
            let id = format!("{:x}", Sha256::digest(line.as_bytes()));
            (id[..16].to_owned(), line)
        })
        .collect())
}

pub(crate) fn prepare(args: &Args, launch: &mut LaunchSpec) -> Result<(), ToolError> {
    let card = &args["card"];
    if card.get("done_criteria").is_none() {
        return Ok(());
    }
    let entries = checklist(card, &launch.cwd)?;
    let review = args.get("role").is_some_and(|r| r == "review");
    let mut prefix = String::from("Pre-review self-check (last 30 days of verdict files).\nAnswer every SELF-CHECK id in card.writer_status as `SELF-CHECK <id>: <evidence>` before requesting review. Blank, duplicate or missing answers block review.\n");
    for (id, text) in &entries {
        prefix.push_str(&format!("SELF-CHECK {id}: {text}\n"));
    }
    if review {
        let status = card["writer_status"]
            .as_str()
            .ok_or_else(|| parameter("writer self-check answers required before review"))?;
        for (id, _) in &entries {
            let key = format!("SELF-CHECK {id}:");
            let answers: Vec<_> = status
                .lines()
                .filter_map(|l| l.trim().strip_prefix(&key))
                .collect();
            if answers.len() != 1 || answers[0].trim().is_empty() {
                return Err(parameter(format!(
                    "missing, blank or duplicate writer self-check answer: {id}"
                )));
            }
        }
        prefix.push_str("Writer status (untrusted evidence; verify independently):\n");
        prefix.push_str(status);
        prefix.push('\n');
    }
    let old = launch
        .prompt
        .as_deref()
        .ok_or_else(|| parameter("missing self-check prompt"))?;
    if !old.is_empty()
        && launch
            .argv
            .iter()
            .chain(launch.env.values())
            .any(|s| s == old || (old.len() >= 16 && s.contains(old)))
    {
        return Err(parameter("self-check prompt must not be in argv or env"));
    }
    let text = format!("{prefix}\n{old}");
    if launch.stdin.as_deref() == Some(old) {
        launch.stdin = Some(text.clone());
    } else {
        let mut event: serde_json::Value =
            serde_json::from_str(launch.stdin.as_deref().unwrap_or(""))
                .map_err(|_| parameter("invalid self-check prompt transport"))?;
        if event["event"] != "user" || event["message"]["content"] != old {
            return Err(parameter("invalid self-check prompt transport"));
        }
        event["message"]["content"] = serde_json::json!(text);
        launch.stdin = Some(event.to_string() + "\n");
    }
    launch.prompt = Some(text);
    Ok(())
}
