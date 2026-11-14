use crate::digest::Runner;
use crate::digest_text::{glob, to_dt};
use crate::fleet_name::string;
use crate::handoff::{re, Card};
use crate::summary_sources::{entry, Entry};
use chrono::{DateTime, TimeDelta, Utc};
use serde_json::Value;
use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

pub struct Config {
    pub home: PathBuf,
    pub data: PathBuf,
    pub routing: PathBuf,
    pub worklog: PathBuf,
    pub keeper: Option<PathBuf>,
    pub now: DateTime<Utc>,
    pub since: String,
    pub project: Option<String>,
}
impl Config {
    pub fn isolated(root: &Path) -> Self {
        Self {
            home: root.join("home"),
            data: root.join("routing"),
            routing: root.join("rstate"),
            worklog: root.join("fleet/worklog.jsonl"),
            keeper: None,
            now: Utc::now(),
            since: "24h".into(),
            project: None,
        }
    }
    pub fn environment() -> Self {
        Self {
            home: crate::home(),
            data: std::env::var_os("WORK_SUMMARY_HOME")
                .filter(|s| !s.is_empty())
                .map_or_else(crate::data_home, Into::into),
            routing: crate::fleet_name::routing_state(),
            worklog: crate::handoff::worklog_path(),
            keeper: std::env::var_os("WORK_SUMMARY_KEEPER_EXPORT")
                .filter(|s| !s.is_empty())
                .map(Into::into),
            now: Utc::now(),
            since: "24h".into(),
            project: None,
        }
    }
}
pub fn parse_since(spec: &str, now: DateTime<Utc>) -> Result<DateTime<Utc>, String> {
    let bad = || {
        format!(
            "bad --since {}: use 24h, <n>h, <n>d or an ISO time",
            crate::summary_sources::py_repr(spec)
        )
    };
    if let Some(c) = re(r"^(\d+)([hd])$").captures(spec.trim()) {
        let n = c[1].parse::<i64>().map_err(|_| bad())?;
        let delta = if &c[2] == "h" {
            TimeDelta::try_hours(n)
        } else {
            TimeDelta::try_days(n)
        }
        .ok_or_else(bad)?;
        now.checked_sub_signed(delta).ok_or_else(bad)
    } else {
        to_dt(spec.trim()).ok_or_else(bad)
    }
}
pub fn cut(s: &str, n: usize) -> String {
    let s = s.split_whitespace().collect::<Vec<_>>().join(" ");
    crate::digest_text::truncate(&s, n)
}
pub fn project_of_session(
    name: &str,
    specs: &BTreeMap<String, Value>,
    projects: &[Value],
) -> Option<String> {
    for pr in projects {
        if name == string(pr, "orchestrator")
            || pr["tracks"]
                .as_array()
                .into_iter()
                .flatten()
                .any(|v| v.as_str() == Some(name))
        {
            return Some(string(pr, "id").into());
        }
    }
    specs
        .get(name)
        .and_then(|s| crate::projects::of_path(string(s, "cwd"), projects))
        .or_else(|| {
            let pre = re(r"-[Ll]\d").split(name).next().unwrap_or(name).to_owned();
            projects
                .iter()
                .any(|pr| string(pr, "id") == pre)
                .then_some(pre)
        })
}
pub fn split_key(text: &str, known: &HashSet<String>) -> (Option<String>, String) {
    let (head, rest) = text.split_once(' ').unwrap_or((text, ""));
    if let Some((p, key)) = head.split_once(':') {
        if known.contains(p) {
            return (Some(p.into()), format!("{key} {rest}").trim().into());
        }
    }
    (None, text.into())
}
struct Fresh {
    session: String,
    ts: DateTime<Utc>,
    path: PathBuf,
    doc: Card,
}
fn cards(
    c: &Config,
    specs: &BTreeMap<String, Value>,
    since: DateTime<Utc>,
) -> (Vec<Fresh>, usize, usize, usize) {
    let mut cands = glob(&c.data.join("status"), ".card.md");
    for spec in specs.values() {
        let base = Path::new(
            spec["cwd"]
                .as_str()
                .filter(|s| !s.is_empty())
                .unwrap_or("/"),
        );
        for key in ["card", "handoff"] {
            let value = &spec[key];
            if !crate::fleet_name::truth(value) {
                continue;
            }
            let val = crate::digest_sources::py(value);
            let p = Path::new(&val);
            let p = if p.is_absolute() || val.starts_with('/') {
                p.to_owned()
            } else {
                base.join(p)
            };
            cands.push(if key == "handoff" {
                crate::digest_card::sibling(&p, true)
            } else {
                p
            });
        }
    }
    let mut seen = HashSet::new();
    let paths: Vec<_> = cands
        .into_iter()
        .filter(|p| {
            p.is_file()
                && !crate::paths::secret_at(p, &c.home)
                && p.canonicalize().is_ok_and(|p| seen.insert(p))
        })
        .collect();
    let total = paths.len();
    let mut best: Vec<Fresh> = vec![];
    let mut legacy = 0;
    for path in paths {
        let Ok(text) = crate::handoff::read_card_at(&path, &c.home) else {
            continue;
        };
        let doc = Card::parse(&text);
        let Some(ts) = doc.stamp().map(|t| t.and_utc()) else {
            legacy += 1;
            continue;
        };
        let session = doc.header.as_ref().unwrap().1.clone();
        let value = Fresh {
            session: session.clone(),
            ts,
            path,
            doc,
        };
        if let Some(old) = best.iter_mut().find(|b| b.session == session) {
            if ts > old.ts {
                *old = value;
            }
        } else {
            best.push(value);
        }
    }
    let stale = best.iter().filter(|b| b.ts < since).count();
    best.retain(|b| b.ts >= since);
    (best, stale, legacy, total)
}
fn section_items(doc: &Card, name: &str) -> Vec<String> {
    doc.rows(name)
        .iter()
        .map(|(_, line)| {
            re(r"^(?:[-*]|\d+[.)])\s+")
                .replace(line.trim(), "")
                .into_owned()
        })
        .filter(|s| !s.is_empty() && s != "none")
        .collect()
}
pub fn merge_done(batches: Vec<Vec<Entry>>) -> Vec<Entry> {
    let mut seen_ref = HashSet::new();
    let mut seen_item = HashSet::new();
    let mut out = vec![];
    for e in batches.into_iter().flatten() {
        if e.refs.iter().any(|r| seen_ref.contains(r))
            || !seen_item.insert((e.project.clone(), e.item.clone()))
        {
            continue;
        }
        seen_ref.extend(e.refs.iter().cloned());
        out.push(e);
    }
    out
}
pub fn fit(texts: &[String], width: usize) -> String {
    for k in (2..=texts.len()).rev() {
        let mut line = texts[..k].join("; ");
        if k < texts.len() {
            line += &format!(" +{}", texts.len() - k);
        }
        if line.chars().count() <= width {
            return line;
        }
    }
    texts.first().map_or(String::new(), |first| {
        first.clone()
            + &if texts.len() > 1 {
                format!(" +{}", texts.len() - 1)
            } else {
                String::new()
            }
    })
}
pub fn build(c: &Config, runner: &dyn Runner) -> Result<Vec<String>, String> {
    let since = parse_since(&c.since, c.now)?;
    let only = c.project.as_deref();
    let projects = crate::projects::load(&c.data.join("projects.yaml"));
    let mut specs = BTreeMap::new();
    for p in glob(&c.routing.join("launch"), ".json") {
        if let Some(d) = crate::read_json(&p, 64 << 20).filter(Value::is_object) {
            specs.insert(p.file_stem().unwrap().to_string_lossy().into_owned(), d);
        }
    }
    let kp = crate::summary_sources::keeper(c, &projects, since);
    let known: HashSet<String> = projects
        .iter()
        .map(|p| string(p, "id").to_owned())
        .chain(kp.projects.iter().cloned())
        .collect();
    let (fresh, stale, legacy, npaths) = cards(c, &specs, since);
    let mut cdone = vec![];
    let mut rest: [Vec<(String, String)>; 3] = std::array::from_fn(|_| vec![]);
    for f in &fresh {
        let sproj = project_of_session(&f.session, &specs, &projects)
            .or_else(|| crate::projects::of_path(&f.path.to_string_lossy(), &projects))
            .unwrap_or("other".into());
        for e in f.doc.entries() {
            let Some(ts) = to_dt(string(&e, "ts")).filter(|t| *t >= since) else {
                continue;
            };
            let (proj, key) = split_key(string(&e, "item"), &known);
            cdone.push(entry(
                &proj.unwrap_or_else(|| sproj.clone()),
                &key,
                &e["ref"],
                ts,
                None,
            ));
        }
        for (i, name, width) in [(0, "In flight", 90), (1, "Open", 90), (2, "Planned", 70)] {
            for text in section_items(&f.doc, name) {
                let (proj, text) = split_key(&text, &known);
                rest[i].push((
                    proj.unwrap_or_else(|| sproj.clone()),
                    cut(&text.replace(" next check ", " next "), width),
                ));
            }
        }
    }
    let (gdone, repos, bad) = crate::summary_sources::git(&projects, since, only, runner);
    let done = merge_done(vec![
        crate::summary_sources::worklog(c, since, &projects, &specs, &known),
        cdone,
        kp.done,
        gdone,
    ]);
    let mut groups: BTreeMap<String, [Vec<String>; 4]> = BTreeMap::new();
    let mut done = done;
    done.sort_by_key(|e| std::cmp::Reverse(e.ts));
    for e in done {
        if only.is_none_or(|p| e.project == p) {
            groups.entry(e.project).or_default()[0].push(format!(
                "{} {} {}",
                e.item,
                e.reference.as_deref().unwrap_or("-"),
                e.ts.format("%m-%dT%H:%MZ")
            ));
        }
    }
    for (i, rows) in rest.into_iter().enumerate() {
        for (proj, text) in rows {
            if only.is_none_or(|p| proj == p) {
                let list = &mut groups.entry(proj).or_default()[i + 1];
                if !list.contains(&text) {
                    list.push(text);
                }
            }
        }
    }
    groups.retain(|_, g| g.iter().any(|a| !a.is_empty()));
    let n: [usize; 4] = std::array::from_fn(|i| groups.values().map(|g| g[i].len()).sum());
    let header=format!("work-summary since {}{}: done {}, in flight {}, open {}, planned {} | projects {}, cards {}/{npaths}, repos {repos}, keeper {}",since.format("%m-%dT%H:%MZ"),only.map_or(String::new(),|p|format!(" project {p}")),n[0],n[1],n[2],n[3],groups.len(),fresh.len(),kp.generated.map_or("off".into(),|t|t.format("%H:%MZ").to_string()));
    let mut notes = kp.note.into_iter().collect::<Vec<_>>();
    let mut extra = vec![];
    if stale > 0 {
        extra.push(format!("{stale} stale card(s)"));
    }
    if legacy > 0 {
        extra.push(format!("{legacy} old-format card(s) skipped"));
    }
    if !bad.is_empty() {
        extra.push(format!("git failed: {}", bad.join(", ")));
    }
    if let Some(p) = only.filter(|p| !known.contains(*p)) {
        extra.push(format!("project {p} is not in projects.yaml"));
    }
    if !extra.is_empty() {
        notes.push(cut(&format!("note: {}", extra.join("; ")), 200));
    }
    let mut groups: Vec<_> = groups.into_iter().collect();
    groups.sort_by_key(|(p, g)| {
        (
            std::cmp::Reverse(g[0].len()),
            std::cmp::Reverse(g[1..].iter().map(Vec::len).sum::<usize>()),
            p.clone(),
        )
    });
    let blocks: Vec<_> = groups
        .into_iter()
        .map(|(proj, g)| {
            let mut block = vec![];
            for (kind, list) in ["done", "flight", "open", "next"].iter().zip(g) {
                if !list.is_empty() {
                    let head = format!("{proj} {kind} {}: ", list.len());
                    block.push(
                        head.clone() + &fit(&list, 200usize.saturating_sub(head.chars().count())),
                    );
                }
            }
            (proj, block)
        })
        .collect();
    let room = 39usize.saturating_sub(notes.len());
    let cap = if blocks.iter().map(|(_, b)| b.len()).sum::<usize>() <= room {
        room
    } else {
        room.saturating_sub(1)
    };
    let mut lines = vec![];
    for (i, (_, block)) in blocks.iter().enumerate() {
        if lines.len() + block.len() > cap {
            lines.push(cut(
                &format!(
                    "+{} more projects (use --project): {}",
                    blocks.len() - i,
                    blocks[i..]
                        .iter()
                        .map(|(p, _)| p.as_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                ),
                200,
            ));
            break;
        }
        lines.extend(block.iter().cloned());
    }
    Ok(std::iter::once(header).chain(notes).chain(lines).collect())
}
pub use crate::summary_cli::main;
