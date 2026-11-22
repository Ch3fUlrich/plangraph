use super::handoff_paths;
use crate::session::{
    paths::{read_text, resolve, var, Paths},
    policy, role_of, Request,
};
use chrono::NaiveDateTime;
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

pub(super) struct Card {
    pub path: PathBuf,
    pub handoff: PathBuf,
}

pub(super) fn newest(request: &Request, paths: &Paths) -> Result<Option<Card>, String> {
    let args = &request.args;
    let cwd = Path::new(&args.cwd);
    let absolute = |s: &str| absolute(cwd, &paths.home, s);
    let ready = ready_marker(&request.spec, paths);
    let mut candidates = BTreeMap::new();
    let mut directories = BTreeSet::from([cwd.join("status")]);
    let mut referenced_card = None;
    if let Some(router) = paths.ops.parent() {
        directories.insert(router.join("status"));
    }

    // A ready marker names the watchdog's checked pair. The launch record and
    // explicit card can also point outside the role's status directory.
    for (card, handoff) in [
        (
            request.spec["card"].as_str(),
            request.spec["handoff"].as_str(),
        ),
        (args.option("card"), args.option("handoff")),
        (ready["card"].as_str(), ready["handoff"].as_str()),
    ] {
        let handoff = handoff.filter(|s| !s.is_empty()).map(absolute);
        let card = card.filter(|s| !s.is_empty()).map(absolute);
        if let Some(path) = &card {
            referenced_card = Some(path.clone());
        }
        let card = card.or_else(|| handoff.as_ref().map(|h| h.with_extension("card.md")));
        if let Some(card) = card {
            if let Some(parent) = card.parent() {
                directories.insert(parent.to_owned());
            }
            if handoff.is_some() {
                candidates.insert(card, handoff);
            } else {
                candidates.entry(card).or_insert(None);
            }
        }
    }
    for entry in directories
        .iter()
        .filter_map(|dir| fs::read_dir(dir).ok())
        .flatten()
        .flatten()
    {
        let path = entry.path();
        if path
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| n.ends_with(".card.md"))
        {
            candidates.entry(resolve(&path)).or_insert(None);
        }
    }
    let selected = candidates
        .into_iter()
        .filter_map(|(path, handoff)| {
            let info = fs::metadata(&path).ok()?;
            if !info.is_file() {
                return None;
            }
            let text = read_text(&path).ok()?;
            let header = card_header(&text);
            let (session, stamp) = header.unwrap_or_default();
            if !belongs(&args.role, &path, session) {
                return None;
            }
            let date = NaiveDateTime::parse_from_str(stamp, "%Y-%m-%dT%H:%MZ").ok();
            Some((date, info.modified().ok(), path, handoff, text))
        })
        .max_by(|a, b| (&a.0, &a.1, &a.2).cmp(&(&b.0, &b.1, &b.2)));
    let Some((_, _, path, associated, text)) = selected else {
        return match referenced_card {
            Some(path) => Err(format!(
                "no readable card for {} (referenced {})",
                args.role,
                path.display()
            )),
            None => Ok(None),
        };
    };
    let sibling = path
        .file_name()
        .and_then(|name| name.to_str())
        .and_then(|name| name.strip_suffix(".card.md"))
        .map(|name| path.with_file_name(format!("{name}.md")));
    let drilldown = text
        .lines()
        .skip_while(|line| line.trim() != "## Drill-down")
        .skip(1)
        .take_while(|line| !line.starts_with("## "));
    let declared = drilldown.flat_map(handoff_paths).next();
    let handoff = declared
        .map(absolute)
        .or_else(|| sibling.filter(|p| p.is_file()))
        .or_else(|| associated.filter(|p| p.is_file()))
        .filter(|p| p.is_file())
        .ok_or_else(|| format!("card {} has no readable handoff", path.display()))?;
    Ok(Some(Card { path, handoff }))
}

fn card_header(card: &str) -> Option<(&str, &str)> {
    let (_, fields) = card.lines().next()?.split_once(" card · ")?;
    let (session, stamp) = fields.split_once('·').unwrap_or((fields, ""));
    Some((session.trim(), stamp.trim()))
}

fn belongs(role: &str, path: &Path, session: &str) -> bool {
    if !session.is_empty() {
        return role_of(session) == role;
    }
    path.file_name()
        .and_then(|n| n.to_str())
        .and_then(|n| n.strip_suffix(".card.md"))
        .is_some_and(|n| {
            n == role
                || n.strip_prefix(role)
                    .and_then(|s| s.strip_prefix('-'))
                    .is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
        })
}

pub(super) fn absolute(cwd: &Path, home: &Path, path: &str) -> PathBuf {
    let path = path
        .strip_prefix("~/")
        .map_or_else(|| cwd.join(path), |rest| home.join(rest));
    resolve(&path)
}

fn ready_marker(spec: &Value, paths: &Paths) -> Value {
    let Some(session) = spec["session_id"]
        .as_str()
        .filter(|s| s.len() == 36 && s.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-'))
    else {
        return Value::Null;
    };
    let guard = var("AUTOOS_CTX_STATE")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            var("XDG_STATE_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| paths.home.join(".local/state"))
                .join("autoos/ctx-guard")
        });
    policy::load(&guard.join(format!("{session}.ready.json"))).unwrap_or_default()
}
