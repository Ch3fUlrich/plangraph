//! Pure define-time plan quality checks. Commands are inspected, never executed.
use regex::Regex;
use serde::Serialize;
use serde_json::Value;
use std::collections::HashSet;
mod similarity;
pub use similarity::sequence_ratio;

#[derive(Debug, Clone, Serialize)]
pub struct Finding {
    pub code: String,
    pub message: String,
}
#[derive(Debug, Clone, Serialize)]
pub struct Report {
    pub key: String,
    pub score: u8,
    pub codes: Vec<String>,
    pub findings: Vec<Finding>,
    pub message: String,
}
fn text(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}
fn list(v: &Value) -> &[Value] {
    v.as_array().map(Vec::as_slice).unwrap_or(&[])
}
fn matches(pattern: &str, value: &str) -> bool {
    Regex::new(pattern).expect("constant regex").is_match(value)
}
fn field<'a>(item: &'a Value, name: &str) -> &'a Value {
    item["card"].get(name).unwrap_or(&item[name])
}
fn rows(item: &Value) -> &[Value] {
    list(field(item, "criteria"))
}
fn criteria_text(item: &Value) -> String {
    if rows(item).is_empty() {
        return text(&item["done_criteria"]).to_lowercase();
    }
    rows(item)
        .iter()
        .map(|r| text(&r["text"]).to_lowercase())
        .collect::<Vec<_>>()
        .join("\n")
}
fn key(item: &Value) -> &str {
    text(item.get("id").unwrap_or(&item["key"]))
}
fn path_valid(path: &str) -> bool {
    !path.is_empty()
        && !path.starts_with('/')
        && !path.contains(['\\', ':', '\0'])
        && path.split('/').all(|p| !["", ".", ".."].contains(&p))
        && globset::Glob::new(path).is_ok()
}
fn prefix(path: &str) -> &str {
    path.split(['*', '?', '['])
        .next()
        .unwrap_or("")
        .trim_end_matches('/')
}
fn overlaps(a: &str, b: &str) -> bool {
    let glob =
        |p: &str, s: &str| globset::Glob::new(p).is_ok_and(|g| g.compile_matcher().is_match(s));
    let (ap, bp) = (prefix(a), prefix(b));
    glob(a, b)
        || glob(b, a)
        || ap.is_empty()
        || bp.is_empty()
        || ap == bp
        || ap.starts_with(&format!("{bp}/"))
        || bp.starts_with(&format!("{ap}/"))
}
fn sets_overlap(a: &Value, b: &Value) -> bool {
    list(a)
        .iter()
        .any(|a| list(b).iter().any(|b| overlaps(text(a), text(b))))
}
/// Syntactic shell check only; no executable lookup or command execution.
fn runnable(command: &str) -> bool {
    if command.trim().is_empty() || command.contains(['\0', '\n', '\r']) {
        return false;
    }
    let mut quote = None;
    let mut escaped = false;
    for c in command.chars() {
        if escaped {
            escaped = false;
        } else if c == '\\' && quote != Some('\'') {
            escaped = true;
        } else if quote == Some(c) {
            quote = None;
        } else if quote.is_none() && ['\'', '"'].contains(&c) {
            quote = Some(c);
        }
    }
    quote.is_none()
        && !escaped
        && !matches(r"[|&;<>]\s*$", command)
        && matches(r"^[\w./-]+(?:\s|$)", command.trim())
        && !matches(
            r"(?i)^(?:CI green|tests? pass|TBD|none|todo)$",
            command.trim(),
        )
}
const WHY: &str = r"(?i)\bD-\d+\b|\bplan(?:\.md)?(?:\s+section|\s*§|#)\s*[\w.-]+";
const HOW: &str = r"(?:[\w.-]+/)+[\w.*?-]+|\b[\w-]+\.(?:rs|py|md|json|toml|yaml)\b";
const ENUM: &str = r"\[[^\]]+\]|:\s*\S+[,|]\s*\S+";
const COUNT: &str =
    r"(?i)\b\d+\s+(?:\w+\s+)?(?:fields|items|keys|rows|cases|paths|entries|rules|roles|checks)\b";
const EXTERNAL: &str = r"(?i)\b(listed|defined|stored|elsewhere)\b|\b(?:from|in)\s+(?:[\w.-]+/|[\w.-]+\.(?:md|json|yaml)|the\s+\w+\s+list)";
const VAGUE: &str = r"(?i)\b(improve|proper|robust|ensure|investigate|consider|etc|works)\b";
const PROCESS: &str = r"(?i)\b(CI green|seat ACCEPT|\d+ seats?)\b|^\s*merged[.,;]?\s*$";
fn fix(code: &str) -> &str {
    match code {
        "title_missing" => "supply a title",
        "title_long" => "limit title to 120 characters",
        "title_dup" => "give this item a unique title",
        "owner_unknown" => "choose an allowed owner",
        "track_missing" => "set or inherit a track",
        "why_missing" => "cite a D-id or plan section in Why:",
        "how_missing" => "name file paths in How:",
        "count_range" => "supply 3 to 8 criteria rows",
        "no_code_row" => "add a code criterion",
        "clause_no_check" => "give each clause id, text, check_ref and verifier class",
        "vague_word" => "state concrete values and error cases",
        "process_only" => "replace process text with a product state",
        "unenumerated_universal" => "enumerate the all/every cases",
        "stated_count" => "derive external list size from its source",
        "no_check" => "supply ordered check_cmd commands",
        "check_not_runnable" => "use complete commands with balanced quotes",
        "bad_path" => "use relative paths without traversal",
        "too_many_write_paths" => "use at most six path slots; globs cost three",
        "write_overlap" => "remove paths reserved by running cards or wait",
        "too_big" => "estimate at most four hours or split under a gate",
        "edge_no_reason" => "drop ordering edges or read dependency outputs; decision/GO allowed",
        "followup_key" => "put findings in the original item's fix log",
        "duplicate_criteria" => "merge duplicate work or make criteria similarity below 0.85",
        _ => unreachable!("known lint code"),
    }
}
/// Lint one item against a complete peer snapshot. Running peers reserve paths.
pub fn lint_item(item: &Value, peers: &[Value]) -> Report {
    lint_with_running(item, peers, &[])
}
fn lint_with_running(item: &Value, peers: &[Value], running: &[Value]) -> Report {
    let mut findings = Vec::new();
    let mut seen = HashSet::new();
    let mut check = |fails: bool, code: &str| {
        if fails && seen.insert(code.to_owned()) {
            findings.push(Finding {
                code: code.into(),
                message: format!("{code}: fix: {}", fix(code)),
            });
        }
    };
    let title = text(&item["title"]);
    let track = text(&item["track"]);
    let owner = text(&item["owner"]);
    let owner_valid = matches(
        r"^(build-L1|plangraph-replan|routing|planner|operator)$",
        owner,
    );
    let title_dup = peers
        .iter()
        .any(|p| key(p) != key(item) && text(&p["title"]).eq_ignore_ascii_case(title));
    let desc = text(&item["description"]);
    let why = desc
        .split("Why:")
        .nth(1)
        .unwrap_or("")
        .split("How:")
        .next()
        .unwrap_or("");
    let how = desc.split("How:").nth(1).unwrap_or("");
    for (fails, code) in [
        (title.trim().is_empty(), "title_missing"),
        (title.chars().count() > 120, "title_long"),
        (title_dup, "title_dup"),
        (!owner_valid, "owner_unknown"),
        (track.trim().is_empty(), "track_missing"),
        (!matches(WHY, why), "why_missing"),
        (!matches(HOW, how), "how_missing"),
    ] {
        check(fails, code);
    }
    let criteria = rows(item);
    let cmds: Vec<_> = match field(item, "check_cmd") {
        Value::String(s) if !s.trim().is_empty() => vec![s.as_str()],
        Value::Array(a) => a.iter().map(text).collect(),
        _ => Vec::new(),
    };
    check(!(3..=8).contains(&criteria.len()), "count_range");
    check(
        !criteria.iter().any(|r| text(&r["class"]) == "code"),
        "no_code_row",
    );
    let indexed = Regex::new(r"^check\[(\d+)\](?:::[\s\S]*)?$").expect("constant regex");
    for row in criteria {
        let clause = text(&row["text"]);
        let reference = text(&row["check_ref"]);
        let valid_ref = reference.split(',').all(|r| match r.trim() {
            "heldout_check" => !text(field(item, "heldout_check")).is_empty(),
            r if !r.starts_with("check[") => !r.is_empty(),
            r => indexed
                .captures(r)
                .and_then(|c| c[1].parse::<usize>().ok())
                .is_some_and(|n| n < cmds.len()),
        });
        let clauses = clause.split(';').filter(|c| !c.trim().is_empty()).count();
        let missing = text(&row["id"]).is_empty()
            || clause.trim().is_empty()
            || !valid_ref
            || !["code", "judged", "attested"].contains(&text(&row["class"]))
            || clauses > reference.split(',').count();
        check(missing, "clause_no_check");
        check(matches(VAGUE, clause), "vague_word");
        check(matches(PROCESS, clause), "process_only");
        check(
            matches(r"(?i)\b(all|every)\b", clause) && !matches(ENUM, clause),
            "unenumerated_universal",
        );
        check(
            matches(COUNT, clause) && matches(EXTERNAL, clause) && !matches(r"\[[^\]]+\]", clause),
            "stated_count",
        );
    }
    check(cmds.is_empty(), "no_check");
    check(cmds.iter().any(|c| !runnable(c)), "check_not_runnable");
    let paths = list(field(item, "write_set"));
    check(
        paths.is_empty() || paths.iter().any(|p| !path_valid(text(p))),
        "bad_path",
    );
    let weight: usize = paths
        .iter()
        .map(|p| 1 + 2 * usize::from(text(p).contains(['*', '?', '['])))
        .sum();
    check(weight > 6, "too_many_write_paths");
    let active = peers
        .iter()
        .filter(|p| matches!(text(&p["status"]), "claimed" | "running"));
    let overlap = running.iter().chain(active).any(|p| {
        key(p) != key(item) && sets_overlap(field(item, "write_set"), field(p, "write_set"))
    });
    check(overlap, "write_overlap");
    let estimate = item
        .get("estimate_h")
        .or_else(|| item.get("est_h"))
        .and_then(Value::as_f64);
    check(
        estimate.is_none_or(|n| !(0.0..=4.0).contains(&n)),
        "too_big",
    );
    for dep in list(&item["depends_on"]) {
        let valid = peers.iter().find(|p| key(p) == text(dep)).is_some_and(|p| {
            text(&p["kind"]) == "decision"
                || key(p) == "GO"
                || key(p).ends_with("-GO")
                || sets_overlap(field(p, "write_set"), field(item, "read_set"))
        });
        check(!valid, "edge_no_reason");
    }
    check(
        matches(r"(?i)-(LOWS|FIX|FU|FOLLOWUP|SQ\d+|R\d+)$", key(item)),
        "followup_key",
    );
    let content = criteria_text(item);
    let duplicate = !content.is_empty()
        && !track.is_empty()
        && peers.iter().any(|p| {
            key(p) != key(item)
                && text(&p["track"]) == track
                && !criteria_text(p).is_empty()
                && sequence_ratio(&content, &criteria_text(p)) >= 0.85
        });
    check(duplicate, "duplicate_criteria");
    let codes: Vec<_> = findings.iter().map(|f| f.code.clone()).collect();
    let mut details: Vec<_> = findings.iter().map(|f| f.message.as_str()).collect();
    if details.is_empty() {
        details.push("ready");
    }
    let message = format!("{}: {}", key(item), details.join("; "));
    Report {
        key: key(item).into(),
        score: 10u8.saturating_sub(codes.len() as u8),
        codes,
        findings,
        message,
    }
}
/// Accept a JSON item array or an object with items and optional running arrays.
pub fn lint_snapshot(snapshot: &Value) -> Vec<Report> {
    let items = snapshot
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_else(|| list(&snapshot["items"]));
    items
        .iter()
        .map(|item| lint_with_running(item, items, list(&snapshot["running"])))
        .collect()
}
