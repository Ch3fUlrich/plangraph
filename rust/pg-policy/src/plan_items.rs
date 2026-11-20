//! Advisory coverage of keeper keys named in PR or part text.
use regex::Regex;
use std::{collections::BTreeSet, io::Read};

pub fn run(args: &[String]) -> Result<Vec<String>, String> {
    if args.len() != 2 {
        return Err("--plan-item-trailers needs BASE HEAD".into());
    }
    let mut text = String::new();
    std::io::stdin()
        .read_to_string(&mut text)
        .map_err(|e| e.to_string())?;
    // Validate revisions even when no key is named; malformed ranges are errors.
    let base = super::git(&["rev-parse", "--verify", &format!("{}^{{commit}}", args[0])])?;
    let head = super::git(&["rev-parse", "--verify", &format!("{}^{{commit}}", args[1])])?;
    // Git parses final trailers; prose, legacy Closes and merge-only trailers
    // cannot stand in for a writer commit's Plan-Item.
    let trailers = super::git(&[
        "log",
        "--no-merges",
        "--format=%(trailers:key=Plan-Item,valueonly)",
        &format!("{}..{}", base.trim(), head.trim()),
    ])?;
    let covered: BTreeSet<_> = trailers.lines().map(str::trim).collect();
    let key = Regex::new(
        r"\b[A-Za-z0-9_][A-Za-z0-9_.-]*:[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?\b",
    )
    .unwrap();
    let named: BTreeSet<_> = key.find_iter(&text).map(|m| m.as_str()).collect();
    Ok(named
        .into_iter()
        .filter(|key| !covered.contains(key))
        .map(|key| format!("warning: {key}: named in PR/part text but no writer commit carries its Plan-Item trailer"))
        .collect())
}
