//! Reader of `router/operator-turns.tsv` and `router/operator-turns/<sha256>.txt` (format v1,
//! `docs/operator-turns-v1.md`). One validator, [`validate`], takes the bytes of the table and the
//! complete inventory of text entries; [`parse`] runs the table rules alone and [`load`] builds the
//! inventory from a directory. The reader never decodes or prints text-file bytes.
use crate::runner::download::{sha256_bytes, valid_hash};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::Path;

pub const TSV_PATH: &str = "router/operator-turns.tsv";
pub const TEXT_DIR: &str = "router/operator-turns";
pub const HEADER: &str = "v\tturn_sha256\tutc_timestamp\tkind\trelay_of\tsession_uuid\ttranscript_line\tdecision\tlifts\twithdrawn_by";

const COLUMNS: [&str; 10] = [
    "v",
    "turn_sha256",
    "utc_timestamp",
    "kind",
    "relay_of",
    "session_uuid",
    "transcript_line",
    "decision",
    "lifts",
    "withdrawn_by",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Human,
    Relay,
}

/// One released item and the gate it was released from.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Lift {
    pub item: String,
    pub gate: String,
}

/// The exact pin a relay names: a turn identity and the D-id it is pinned to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PinRef {
    pub turn_sha256: String,
    pub utc_timestamp: String,
    pub decision: String,
}

impl fmt::Display for PinRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}@{}@{}",
            self.turn_sha256, self.utc_timestamp, self.decision
        )
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PinnedTurn {
    pub line: usize,
    pub turn_sha256: String,
    pub utc_timestamp: String,
    pub kind: Kind,
    /// Empty for a human row; for a relay the exact source pins, strictly ascending.
    pub relay_of: Vec<PinRef>,
    pub session_uuid: String,
    pub transcript_line: u64,
    pub decision: String,
    pub lifts: Vec<Lift>,
    pub withdrawn_by: Option<String>,
}

impl PinnedTurn {
    /// `<turn_sha256>@<utc_timestamp>`: the reference a D-row carries for each turn pinned to it.
    pub fn turn_ref(&self) -> String {
        format!("{}@{}", self.turn_sha256, self.utc_timestamp)
    }
    pub fn pin_ref(&self) -> PinRef {
        PinRef {
            turn_sha256: self.turn_sha256.clone(),
            utc_timestamp: self.utc_timestamp.clone(),
            decision: self.decision.clone(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Problem {
    pub line: usize,
    pub code: &'static str,
    pub detail: String,
}

impl fmt::Display for Problem {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{TSV_PATH}:{}: {}: {}",
            self.line, self.code, self.detail
        )
    }
}

/// One entry of the inventory: its name inside its directory, whether it is a regular file (no
/// symlink, no directory, no special file) and its bytes (empty when it is not regular).
#[derive(Clone, Debug)]
pub struct Entry {
    pub name: String,
    pub regular: bool,
    pub bytes: Vec<u8>,
}

fn problem(line: usize, code: &'static str, detail: impl Into<String>) -> Problem {
    Problem {
        line,
        code,
        detail: detail.into(),
    }
}

fn field_problem(line: usize, column: &str, detail: &str) -> Problem {
    let code: &'static str = match column {
        "v" => "field:v",
        "turn_sha256" => "field:turn_sha256",
        "utc_timestamp" => "field:utc_timestamp",
        "kind" => "field:kind",
        "relay_of" => "field:relay_of",
        "session_uuid" => "field:session_uuid",
        "transcript_line" => "field:transcript_line",
        "decision" => "field:decision",
        "lifts" => "field:lifts",
        _ => "field:withdrawn_by",
    };
    problem(line, code, format!("{column}: {detail}"))
}

/// Canonical `D-<n>`: digits, no leading zero, fits u64.
fn canonical_decision(value: &str) -> Option<u64> {
    if !crate::router_batch::decision_id(value) {
        return None;
    }
    let n = &value[2..];
    if n.starts_with('0') {
        return None;
    }
    n.parse().ok()
}

/// `YYYY-MM-DDTHH:MM:SS.mmmZ`, a real calendar time, no leap second.
fn valid_time(value: &str) -> bool {
    let b = value.as_bytes();
    if b.len() != 24 {
        return false;
    }
    let shape = b.iter().enumerate().all(|(i, c)| match i {
        4 | 7 => *c == b'-',
        10 => *c == b'T',
        13 | 16 => *c == b':',
        19 => *c == b'.',
        23 => *c == b'Z',
        _ => c.is_ascii_digit(),
    });
    shape && &value[17..19] != "60" && chrono::DateTime::parse_from_rfc3339(value).is_ok()
}

fn valid_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => c == b'-',
            _ => c.is_ascii_digit() || (b'a'..=b'f').contains(&c),
        })
}

fn valid_gate(value: &str) -> bool {
    let mut chars = value.chars();
    chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && value.len() <= 32
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

fn valid_item(value: &str) -> bool {
    match value.split_once(':') {
        Some((project, id)) => {
            !project.is_empty()
                && !id.is_empty()
                && value.trim() == value
                && !value.chars().any(char::is_control)
        }
        None => false,
    }
}

fn parse_pin_ref(value: &str) -> Option<PinRef> {
    let mut parts = value.split('@');
    let (hash, time, decision) = (parts.next()?, parts.next()?, parts.next()?);
    if parts.next().is_some()
        || !valid_hash(hash)
        || !valid_time(time)
        || canonical_decision(decision).is_none()
    {
        return None;
    }
    Some(PinRef {
        turn_sha256: hash.into(),
        utc_timestamp: time.into(),
        decision: decision.into(),
    })
}

/// Comma-joined pin references, at least one, strictly ascending (no duplicate).
fn parse_pin_refs(value: &str) -> Option<Vec<PinRef>> {
    let refs: Vec<PinRef> = value
        .split(',')
        .map(parse_pin_ref)
        .collect::<Option<Vec<_>>>()?;
    let text: Vec<String> = refs.iter().map(PinRef::to_string).collect();
    text.windows(2).all(|w| w[0] < w[1]).then_some(refs)
}

fn parse_lifts(value: &str) -> Result<Vec<Lift>, &'static str> {
    let lifts: Vec<Lift> =
        serde_json::from_str(value).map_err(|_| "not a JSON array of item and gate objects")?;
    if serde_json::to_string(&lifts).ok().as_deref() != Some(value) {
        return Err("not the compact serde_json form (members item then gate, no spaces)");
    }
    let mut seen = BTreeSet::new();
    for lift in &lifts {
        if !valid_item(&lift.item) {
            return Err("item is not <project>:<id> without control characters or outer spaces");
        }
        if !valid_gate(&lift.gate) {
            return Err("gate is not [a-z][a-z0-9_]{0,31}");
        }
        if !seen.insert(lift) {
            return Err("duplicate item and gate pair");
        }
    }
    Ok(lifts)
}

/// One row, field rules only. Every fault is returned, none stops the scan of the others.
fn parse_row(line: usize, text: &str) -> Result<PinnedTurn, Vec<Problem>> {
    let fields: Vec<&str> = text.split('\t').collect();
    if fields.len() != COLUMNS.len() {
        return Err(vec![problem(
            line,
            "columns",
            format!("{} fields, expected {}", fields.len(), COLUMNS.len()),
        )]);
    }
    let mut out = Vec::new();
    // A blank or padded field is reported once; the other fields of the row are still checked.
    let mut blank = [false; 10];
    for (i, (name, value)) in COLUMNS.iter().zip(&fields).enumerate() {
        if value.is_empty() {
            out.push(field_problem(line, name, "empty"));
            blank[i] = true;
        } else if value.trim() != *value {
            out.push(field_problem(line, name, "outer whitespace"));
            blank[i] = true;
        }
    }
    if !blank[0] && fields[0] != "1" {
        out.push(problem(line, "version", "v must be 1"));
    }
    let mut bad = |column: &str, detail: &str| out.push(field_problem(line, column, detail));
    if !blank[1] && !valid_hash(fields[1]) {
        bad("turn_sha256", "not 64 lowercase hex digits");
    }
    if !blank[2] && !valid_time(fields[2]) {
        bad("utc_timestamp", "not YYYY-MM-DDTHH:MM:SS.mmmZ");
    }
    let kind = match fields[3] {
        _ if blank[3] => None,
        "human" => Some(Kind::Human),
        "relay" => Some(Kind::Relay),
        _ => {
            bad("kind", "not human or relay");
            None
        }
    };
    let relay_of = match (kind, fields[4]) {
        _ if blank[4] => Vec::new(),
        (Some(Kind::Human), "-") | (None, _) => Vec::new(),
        (Some(Kind::Human), _) => {
            bad("relay_of", "a human row must hold -");
            Vec::new()
        }
        (Some(Kind::Relay), value) => parse_pin_refs(value).unwrap_or_else(|| {
            bad(
                "relay_of",
                "not strictly ascending <turn_sha256>@<utc_timestamp>@D-<n>, comma-joined",
            );
            Vec::new()
        }),
    };
    if !blank[5] && !valid_uuid(fields[5]) {
        bad("session_uuid", "not a lowercase 8-4-4-4-12 hex uuid");
    }
    let transcript_line = match fields[6].parse::<u64>() {
        _ if blank[6] => 0,
        Ok(n)
            if n >= 1
                && !fields[6].starts_with('0')
                && fields[6].bytes().all(|b| b.is_ascii_digit()) =>
        {
            n
        }
        _ => {
            bad(
                "transcript_line",
                "not a decimal integer >= 1 without leading zero",
            );
            0
        }
    };
    let decision = canonical_decision(fields[7]);
    if !blank[7] && decision.is_none() {
        bad("decision", "not exactly one canonical D-<n>");
    }
    let lifts = match parse_lifts(fields[8]) {
        _ if blank[8] => Vec::new(),
        Ok(lifts) => lifts,
        Err(why) => {
            bad("lifts", why);
            Vec::new()
        }
    };
    let withdrawn_by = if blank[9] || fields[9] == "-" {
        None
    } else {
        match (canonical_decision(fields[9]), decision) {
            (Some(w), Some(d)) if w > d => Some(fields[9].to_string()),
            (Some(_), None) => None, // the decision column is reported already
            _ => {
                bad(
                    "withdrawn_by",
                    "not - or a canonical D-id greater than the decision",
                );
                None
            }
        }
    };
    if !out.is_empty() {
        return Err(out);
    }
    Ok(PinnedTurn {
        line,
        turn_sha256: fields[1].into(),
        utc_timestamp: fields[2].into(),
        kind: kind.unwrap_or(Kind::Human),
        relay_of,
        session_uuid: fields[5].into(),
        transcript_line,
        decision: fields[7].into(),
        lifts,
        withdrawn_by,
    })
}

/// The one serialiser: the row text of a turn, without a trailing LF.
pub fn format_row(turn: &PinnedTurn) -> String {
    let kind = match turn.kind {
        Kind::Human => "human",
        Kind::Relay => "relay",
    };
    let relay_of = if turn.relay_of.is_empty() {
        "-".to_string()
    } else {
        let refs: Vec<String> = turn.relay_of.iter().map(PinRef::to_string).collect();
        refs.join(",")
    };
    let lifts = serde_json::to_string(&turn.lifts).unwrap_or_else(|_| "[]".into());
    [
        "1",
        &turn.turn_sha256,
        &turn.utc_timestamp,
        kind,
        &relay_of,
        &turn.session_uuid,
        &turn.transcript_line.to_string(),
        &turn.decision,
        &lifts,
        turn.withdrawn_by.as_deref().unwrap_or("-"),
    ]
    .join("\t")
}

/// Table rules, no filesystem: syntax of every row and the cross-row rules.
pub fn parse(tsv: &str) -> Result<Vec<PinnedTurn>, Vec<Problem>> {
    if tsv.is_empty() {
        return Err(vec![problem(0, "header", "empty file")]);
    }
    if tsv.contains('\r') || !tsv.ends_with('\n') {
        return Err(vec![problem(
            0,
            "line_ending",
            "LF only and a final LF are required",
        )]);
    }
    let mut problems = Vec::new();
    let mut rows = Vec::new();
    for (i, text) in tsv[..tsv.len() - 1].split('\n').enumerate() {
        let line = i + 1;
        if line == 1 {
            if text != HEADER {
                problems.push(problem(1, "header", "not the version 1 header"));
            }
            continue;
        }
        match parse_row(line, text) {
            Ok(row) => rows.push(row),
            Err(found) => problems.extend(found),
        }
    }
    problems.extend(cross_row(&rows));
    if problems.is_empty() {
        Ok(rows)
    } else {
        problems.sort_by_key(|p| p.line);
        Err(problems)
    }
}

fn cross_row(rows: &[PinnedTurn]) -> Vec<Problem> {
    let mut out = Vec::new();
    let mut identity: BTreeMap<(&str, &str), &PinnedTurn> = BTreeMap::new();
    let mut pins: BTreeMap<(&str, &str, &str), &PinnedTurn> = BTreeMap::new();
    let mut withdrawal: BTreeMap<&str, &Option<String>> = BTreeMap::new();
    for row in rows {
        let key = (row.turn_sha256.as_str(), row.utc_timestamp.as_str());
        match identity.get(&key) {
            Some(first) => {
                if first.kind != row.kind
                    || first.relay_of != row.relay_of
                    || first.session_uuid != row.session_uuid
                    || first.transcript_line != row.transcript_line
                {
                    out.push(problem(
                        row.line,
                        "turn_conflict",
                        format!("{} differs from line {} in kind, relay_of, session_uuid or transcript_line", row.turn_ref(), first.line),
                    ));
                }
            }
            None => {
                identity.insert(key, row);
            }
        }
        let pin = (key.0, key.1, row.decision.as_str());
        if let Some(first) = pins.get(&pin) {
            out.push(problem(
                row.line,
                "duplicate_pin",
                format!(
                    "{} {} already pinned at line {}",
                    row.turn_ref(),
                    row.decision,
                    first.line
                ),
            ));
        } else {
            pins.insert(pin, row);
        }
        match withdrawal.get(row.decision.as_str()) {
            Some(first) if **first != row.withdrawn_by => out.push(problem(
                row.line,
                "withdrawal_conflict",
                format!(
                    "{} carries another withdrawn_by than its first pin",
                    row.decision
                ),
            )),
            Some(_) => {}
            None => {
                withdrawal.insert(row.decision.as_str(), &row.withdrawn_by);
            }
        }
    }
    for row in rows {
        if row.relay_of.is_empty() {
            continue;
        }
        let mut sources: Vec<&PinnedTurn> = Vec::new();
        for source_ref in &row.relay_of {
            let found = pins
                .get(&(
                    source_ref.turn_sha256.as_str(),
                    source_ref.utc_timestamp.as_str(),
                    source_ref.decision.as_str(),
                ))
                .filter(|s| s.kind == Kind::Human && s.utc_timestamp <= row.utc_timestamp);
            match found {
                Some(source) => sources.push(source),
                None => out.push(problem(
                    row.line,
                    "relay_source_missing",
                    format!("no earlier human pin {source_ref}"),
                )),
            }
        }
        if sources.len() != row.relay_of.len() {
            continue;
        }
        // Each relayed lift is held by at least one of the named source pins.
        for lift in &row.lifts {
            if !sources.iter().any(|s| s.lifts.contains(lift)) {
                out.push(problem(
                    row.line,
                    "relay_scope",
                    format!("{} is not a lift of any source pin", lift.item),
                ));
            }
        }
    }
    out
}

fn text_name(hash: &str) -> String {
    format!("{hash}.txt")
}

/// The one validator: the table bytes (an entry, so its own mode is checked) and every entry of
/// the text directory. Text rules run once the table parses.
pub fn validate(tsv: &Entry, texts: &[Entry]) -> Result<Vec<PinnedTurn>, Vec<Problem>> {
    if !tsv.regular {
        return Err(vec![problem(
            0,
            "tsv_not_regular",
            "the table must be a regular file",
        )]);
    }
    let Ok(text) = std::str::from_utf8(&tsv.bytes) else {
        return Err(vec![problem(0, "encoding", "the table is not UTF-8")]);
    };
    if text.starts_with('\u{feff}') {
        return Err(vec![problem(0, "encoding", "the table starts with a BOM")]);
    }
    let rows = parse(text)?;
    let by_name: BTreeMap<&str, &Entry> = texts.iter().map(|e| (e.name.as_str(), e)).collect();
    let mut out = Vec::new();
    let mut named = BTreeSet::new();
    let mut checked = BTreeSet::new();
    for row in &rows {
        let name = text_name(&row.turn_sha256);
        named.insert(name.clone());
        if !checked.insert((&row.turn_sha256, &row.utc_timestamp)) {
            continue;
        }
        match by_name.get(name.as_str()) {
            None => out.push(problem(row.line, "text_missing", row.turn_sha256.clone())),
            Some(e) if !e.regular || e.bytes.is_empty() => out.push(problem(
                row.line,
                "text_not_regular",
                format!("{} is empty or not a regular file", row.turn_sha256),
            )),
            Some(e) if sha256_bytes(&e.bytes) != row.turn_sha256 => out.push(problem(
                row.line,
                "text_hash",
                format!("bytes of {} do not hash to its name", row.turn_sha256),
            )),
            Some(_) => {}
        }
    }
    for e in texts.iter().filter(|e| !named.contains(&e.name)) {
        let detail = match e.name.strip_suffix(".txt") {
            Some(h) if valid_hash(h) => h.to_string(),
            _ => "an entry that is no <turn_sha256>.txt".to_string(),
        };
        out.push(problem(0, "orphan_text", detail));
    }
    if out.is_empty() {
        Ok(rows)
    } else {
        out.sort_by_key(|p| p.line);
        Err(out)
    }
}

fn real_dir(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok_and(|m| m.is_dir() && !m.file_type().is_symlink())
}

fn read_entry(path: &Path, name: String) -> std::io::Result<Entry> {
    let regular = crate::atomic::regular(path).is_ok();
    let bytes = if regular {
        std::fs::read(path)?
    } else {
        Vec::new()
    };
    Ok(Entry {
        name,
        regular,
        bytes,
    })
}

/// A directory checkout: `router/` and `router/operator-turns/` must be real directories and
/// every entry is classified without following links. For fixtures and pre-commit checks; the
/// index builder and CI feed [`validate`] from one pinned Git tree instead (an untracked or
/// modified file is no authority).
pub fn load(data_dir: &Path) -> Result<Vec<PinnedTurn>, Vec<Problem>> {
    let unreadable = |what: &str| vec![problem(0, "unreadable", what.to_string())];
    let router = data_dir.join("router");
    let text_dir = data_dir.join(TEXT_DIR);
    if std::fs::symlink_metadata(&router).is_ok_and(|m| m.file_type().is_symlink()) {
        return Err(vec![problem(0, "path_not_real", "router is a symlink")]);
    }
    if !real_dir(&router) {
        return Err(unreadable("router directory is missing"));
    }
    let tsv_path = data_dir.join(TSV_PATH);
    if std::fs::symlink_metadata(&tsv_path).is_err() {
        return Err(unreadable("the table is missing"));
    }
    let tsv = read_entry(&tsv_path, TSV_PATH.into())
        .map_err(|_| unreadable("the table cannot be read"))?;
    let mut texts = Vec::new();
    match std::fs::symlink_metadata(&text_dir) {
        Err(_) => {}
        Ok(m) if m.file_type().is_symlink() || !m.is_dir() => {
            return Err(vec![problem(
                0,
                "path_not_real",
                "operator-turns is no real directory",
            )]);
        }
        Ok(_) => {
            let listing = std::fs::read_dir(&text_dir)
                .map_err(|_| unreadable("the text directory cannot be read"))?;
            for entry in listing {
                let entry = entry.map_err(|_| unreadable("the text directory cannot be read"))?;
                let name = entry.file_name().to_string_lossy().into_owned();
                texts.push(
                    read_entry(&entry.path(), name)
                        .map_err(|_| unreadable("a text file cannot be read"))?,
                );
            }
            texts.sort_by(|a, b| a.name.cmp(&b.name));
        }
    }
    validate(&tsv, &texts)
}

/// The lifts a decision releases now: its pins are not withdrawn and a relayed lift is held by
/// a live source pin the relay names. A sibling pin on the source turn, never named, does not
/// stand in for it.
pub fn live_lifts<'a>(turns: &'a [PinnedTurn], decision: &str) -> Vec<&'a Lift> {
    let mut out: Vec<&Lift> = Vec::new();
    for row in turns.iter().filter(|t| t.decision == decision) {
        if row.withdrawn_by.is_some() {
            continue;
        }
        for lift in &row.lifts {
            let authorized = row.relay_of.is_empty()
                || row.relay_of.iter().any(|source| {
                    turns.iter().any(|t| {
                        t.pin_ref() == *source
                            && t.kind == Kind::Human
                            && t.withdrawn_by.is_none()
                            && t.lifts.contains(lift)
                    })
                });
            if authorized && !out.contains(&lift) {
                out.push(lift);
            }
        }
    }
    out
}

const CITATION_KEY: &str = "operator-turns=";

/// The statement a D-row carries for the turns pinned to `decision`:
/// `operator-turns=<turn_ref>[,<turn_ref>...]`, refs distinct and ascending; `None` without a pin.
pub fn citation(turns: &[PinnedTurn], decision: &str) -> Option<String> {
    let refs: BTreeSet<String> = turns
        .iter()
        .filter(|t| t.decision == decision)
        .map(PinnedTurn::turn_ref)
        .collect();
    (!refs.is_empty()).then(|| format!("{CITATION_KEY}{}", Vec::from_iter(refs).join(",")))
}

/// The turn refs a D-row cell cites: exactly one whitespace-delimited token that starts with
/// `operator-turns=` (a second one, or any token fault, refuses the cell); `None` when it has none.
pub fn cited_refs(cell: &str) -> Result<Option<Vec<String>>, &'static str> {
    let mut tokens = cell
        .split_whitespace()
        .filter_map(|t| t.strip_prefix(CITATION_KEY));
    let Some(list) = tokens.next() else {
        return Ok(None);
    };
    if tokens.next().is_some() {
        return Err("more than one operator-turns statement");
    }
    let refs: Vec<String> = list.split(',').map(String::from).collect();
    let well_formed = refs.iter().all(|r| {
        r.split_once('@')
            .is_some_and(|(h, t)| valid_hash(h) && valid_time(t))
    });
    if !well_formed || !refs.windows(2).all(|w| w[0] < w[1]) {
        return Err("operator-turns refs are not distinct ascending <turn_sha256>@<utc_timestamp>");
    }
    Ok(Some(refs))
}
