//! Fixed fleet card grammar, shared by the handoff validator and digest.
use chrono::{Duration, NaiveDateTime};
use regex::Regex;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

pub const SECTIONS: [&str; 8] = [
    "Goal",
    "Keeper",
    "Done",
    "In flight",
    "Planned",
    "Open",
    "Traps",
    "Drill-down",
];
const STAMP: &str = "%Y-%m-%dT%H:%MZ";
const HEADER_FORM: &str = "# <role> card · <session-name> · <YYYY-MM-DDTHH:MMZ>";
const DONE_FORM: &str = "- MM-DDTHH:MMZ KEY|D-nnnn sha|PR#n evidence";

pub fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("static regex")
}
pub fn clip(s: &str, n: usize) -> String {
    let s = if s.chars().count() > n {
        s.chars().take(n - 1).collect::<String>() + "…"
    } else {
        s.to_owned()
    };
    serde_json::to_string(&s).unwrap()
}

#[derive(Debug)]
pub struct Card {
    pub lines: Vec<String>,
    pub header: Option<(String, String, String)>,
    pub headings: Vec<(usize, String)>,
    pub body: Vec<(String, Vec<(usize, String)>)>,
}
impl Card {
    pub fn parse(text: &str) -> Self {
        let lines: Vec<String> = text.lines().map(str::to_owned).collect();
        let header = lines.first().and_then(|s| re(r"^# (?P<role>[^<>·]+?) card · (?P<session>[^<>·]+?) · (?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\dZ)$").captures(s.trim_end())).map(|c| (c["role"].to_owned(), c["session"].to_owned(), c["ts"].to_owned()));
        let mut doc = Self {
            lines,
            header,
            headings: vec![],
            body: vec![],
        };
        let mut cur = None;
        for (i, raw) in doc.lines.iter().enumerate() {
            let line = raw.trim_end();
            if let Some(name) = line.strip_prefix("## ") {
                let name = name.trim();
                doc.headings.push((i + 1, name.to_owned()));
                cur = None;
                if SECTIONS.contains(&name) && !doc.body.iter().any(|(s, _)| s == name) {
                    cur = Some(doc.body.len());
                    doc.body.push((name.to_owned(), vec![]));
                }
            } else if !line.trim().is_empty() {
                if let Some(j) = cur {
                    doc.body[j].1.push((i + 1, line.to_owned()));
                }
            }
        }
        doc
    }
    pub fn stamp(&self) -> Option<NaiveDateTime> {
        self.header
            .as_ref()
            .and_then(|h| NaiveDateTime::parse_from_str(&h.2, STAMP).ok())
    }
    pub fn rows(&self, name: &str) -> &[(usize, String)] {
        self.body
            .iter()
            .find(|(n, _)| n == name)
            .map_or(&[], |(_, r)| r)
    }
    pub fn entries(&self) -> Vec<Value> {
        self.rows("Done")
            .iter()
            .filter_map(|(n, s)| check_done(*n, s, self.stamp()).0)
            .collect()
    }
}

fn full_ts(md: &str, base: NaiveDateTime) -> Option<NaiveDateTime> {
    use chrono::Datelike;
    [base.year(), base.year() - 1]
        .into_iter()
        .filter_map(|y| NaiveDateTime::parse_from_str(&format!("{y}-{md}"), STAMP).ok())
        .find(|t| *t <= base + Duration::days(1))
}

fn check_done(n: usize, line: &str, base: Option<NaiveDateTime>) -> (Option<Value>, Vec<String>) {
    if line.trim() == "- none" {
        return (None, vec![]);
    }
    // Python split(None, 3): retain whitespace within the evidence.
    let parts = line.strip_prefix("- ").and_then(|s| {
        re(r"^(\S+)\s+(\S+)\s+(\S+)\s+(.+)$")
            .captures(s.trim())
            .map(|c| {
                (
                    c[1].to_owned(),
                    c[2].to_owned(),
                    c[3].to_owned(),
                    c[4].trim().to_owned(),
                )
            })
    });
    let Some((md, item, reference, ev)) = parts else {
        return (
            None,
            vec![format!(
                "line {n}: Done line must be \"{DONE_FORM}\", got {}",
                clip(line, 60)
            )],
        );
    };
    let mut bad = vec![];
    let mut ts = None;
    if !re(r"^\d\d-\d\dT\d\d:\d\dZ$").is_match(&md)
        || NaiveDateTime::parse_from_str(&format!("2000-{md}"), STAMP).is_err()
    {
        bad.push(format!(
            "line {n}: Done stamp {} must be a UTC MM-DDTHH:MMZ",
            clip(&md, 24)
        ));
    } else if let Some(base) = base {
        ts = full_ts(&md, base);
        if ts.is_none() {
            bad.push(format!(
                "line {n}: Done stamp {md} is not a date in the card's year"
            ));
        }
    }
    if !re(r"^(?:[a-z][a-z0-9-]*:)?[A-Z0-9][A-Z0-9._-]*$").is_match(&item) {
        bad.push(format!(
            "line {n}: Done id {} must be a keeper KEY or D-nnnn",
            clip(&item, 40)
        ));
    }
    if !re(r"^(?:[0-9a-f]{7,40}|PR#\d+|-)$").is_match(&reference) {
        bad.push(format!(
            "line {n}: Done ref {} must be a sha (7-40 hex) or PR#n (- when nothing was committed)",
            clip(&reference, 40)
        ));
    }
    if ["-", "none", "n/a", "na", "tbd", "todo", "?"].contains(&ev.to_lowercase().as_str())
        || (ev.starts_with('<') && ev.ends_with('>'))
    {
        bad.push(format!(
            "line {n}: Done evidence missing (CI run id / test / verdict)"
        ));
    }
    let entry=ts.filter(|_|bad.is_empty()).map(|t|json!({"n":n,"ts":t.format(STAMP).to_string(),"item":item,"ref":if reference=="-"{Value::Null}else{json!(reference)},"evidence":ev}));
    (entry, bad)
}

pub fn check_text(text: &str) -> Vec<String> {
    let doc = Card::parse(text);
    if !doc.lines.iter().any(|s| !s.trim().is_empty()) {
        return vec!["card is empty".into()];
    }
    let mut out = vec![];
    if doc.lines.len() > 40 {
        out.push(format!("card has {} lines, max 40", doc.lines.len()));
    }
    if let Some(h) = &doc.header {
        if doc.stamp().is_none() {
            out.push(format!(
                "line 1: header stamp {} is not a valid UTC date",
                h.2
            ));
        }
    } else {
        out.push(format!(
            "line 1: header must be \"{HEADER_FORM}\", got {}",
            clip(&doc.lines[0], 60)
        ));
    }
    let mut seen = HashMap::new();
    let mut got = vec![];
    for (n, name) in &doc.headings {
        if !SECTIONS.contains(&name.as_str()) {
            out.push(format!("line {n}: unexpected heading \"## {name}\""));
        } else if seen.insert(name, *n).is_some() {
            out.push(format!("line {n}: duplicate heading \"## {name}\""));
        } else {
            got.push(name.as_str());
        }
    }
    for s in SECTIONS {
        if !seen.contains_key(&s.to_owned()) {
            out.push(format!("missing heading \"## {s}\""));
        }
    }
    if got
        != SECTIONS
            .into_iter()
            .filter(|s| got.contains(s))
            .collect::<Vec<_>>()
    {
        out.push(format!(
            "heading order: got {}; expected {}",
            got.join(", "),
            SECTIONS.join(", ")
        ));
    }
    for (name, rows) in &doc.body {
        if rows.is_empty() {
            out.push(format!("section \"## {name}\" is empty: write \"- none\""));
        } else if rows.len() > 1 && rows.iter().any(|(_, s)| s.trim() == "- none") {
            out.push(format!(
                "section \"## {name}\": \"- none\" must be the only line"
            ));
        }
    }
    for (n, line) in doc.rows("Done") {
        out.extend(check_done(*n, line, doc.stamp()).1);
    }
    out
}

pub fn read_card(path: &Path) -> Result<String, String> {
    if crate::paths::secret(path) {
        return Err(format!(
            "refusing to read {}: secret-like file",
            path.file_name().unwrap_or_default().to_string_lossy()
        ));
    }
    let mut raw = vec![];
    let read = fs::File::open(path).and_then(|f| f.take(65537).read_to_end(&mut raw));
    if let Err(e) = read {
        return Err(format!("cannot read {}: {}", path.display(), e));
    }
    if raw.len() > 65536 {
        return Err("card is larger than 65536 bytes (max 40 lines)".into());
    }
    Ok(String::from_utf8_lossy(raw.strip_prefix(&[239, 187, 191]).unwrap_or(&raw)).into_owned())
}

pub fn worklog_path() -> PathBuf {
    std::env::var_os("FLEET_WORKLOG")
        .filter(|s| !s.is_empty())
        .map_or_else(
            || crate::home().join(".local/state/fleet/worklog.jsonl"),
            Into::into,
        )
}

fn record_key(rec: &Value) -> String {
    let s = |k| rec[k].as_str().unwrap_or("");
    format!(
        "{} {} {} {}",
        s("ts").get(5..).unwrap_or(""),
        s("item"),
        rec["ref"].as_str().filter(|s| !s.is_empty()).unwrap_or("-"),
        s("evidence")
    )
}

pub fn log_done(path: &Path, doc: &Card, wl: &Path) -> std::io::Result<usize> {
    let entries = doc.entries();
    if entries.is_empty() {
        return Ok(0);
    }
    if let Some(p) = wl.parent() {
        fs::create_dir_all(p)?;
    }
    let mut options = OpenOptions::new();
    options.read(true).append(true).create(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut f = options.open(wl)?;
    f.lock()?;
    f.seek(SeekFrom::Start(0))?;
    let mut raw = vec![];
    f.read_to_end(&mut raw)?;
    let old = String::from_utf8_lossy(&raw);
    let mut seen: HashSet<String> = old
        .lines()
        .filter_map(|s| serde_json::from_str::<Value>(s).ok())
        .filter(Value::is_object)
        .map(|v| record_key(&v))
        .collect();
    let h = doc.header.as_ref().expect("valid card");
    let src = crate::paths::display(&crate::paths::resolve(path)?);
    let mut new = vec![];
    for e in entries {
        if seen.insert(record_key(&e)) {
            new.push(json!({"ts":e["ts"],"role":h.0,"session":h.1,"item":e["item"],"ref":e["ref"],"evidence":e["evidence"],"src":src}));
        }
    }
    if !new.is_empty() {
        if !old.is_empty() && !old.ends_with('\n') {
            f.write_all(b"\n")?;
        }
        for rec in &new {
            writeln!(f, "{}", crate::json_python(rec))?;
        }
    }
    Ok(new.len())
}

pub fn main(args: &[String]) -> i32 {
    let pos: Vec<_> = args.iter().filter(|s| !s.starts_with("--")).collect();
    if pos.len() != 1 || args.iter().any(|s| s.starts_with("--") && s != "--log") {
        eprintln!(
            "{}",
            include_str!("usage/handoff-check.txt").trim_end_matches('\n')
        );
        return 2;
    }
    let path = Path::new(pos[0]);
    let text = match read_card(path) {
        Ok(s) => s,
        Err(e) => {
            println!("{e}");
            return 1;
        }
    };
    let reasons = check_text(&text);
    if !reasons.is_empty() {
        println!("{}", reasons.join("\n"));
        return 1;
    }
    let doc = Card::parse(&text);
    let mut msg = format!(
        "ok: {} lines, {} Done",
        doc.lines.len(),
        doc.entries().len()
    );
    if args.iter().any(|s| s == "--log") {
        let wl = worklog_path();
        match log_done(path, &doc, &wl) {
            Ok(n) => msg += &format!(", {n} new in {}", wl.display()),
            Err(e) => {
                println!("cannot log to {}: {e}", wl.display());
                return 1;
            }
        }
    }
    println!("{msg}");
    0
}
