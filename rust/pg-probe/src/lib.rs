//! Offline probe fixtures and deterministic scoring; no session or process launcher.
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    fmt,
    io::BufRead,
};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Rule,
    Knowledge,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RetrievalPath {
    Tool(String),
    FileClass(String),
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct Probe {
    pub id: String,
    pub kind: Kind,
    pub question: String,
    pub gold: String,
    pub expected_path: RetrievalPath,
    pub planner_proposed: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub enum LoadError {
    Io { line: usize },
    InvalidJson { line: usize },
    DuplicateId { line: usize, id: String },
    MissingGold { line: usize },
    UnknownKind { line: usize, kind: String },
}

impl fmt::Display for LoadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for LoadError {}

pub fn load(reader: impl BufRead) -> Result<Vec<Probe>, LoadError> {
    let mut probes = Vec::new();
    let mut ids = BTreeSet::new();
    for (index, row) in reader.lines().enumerate() {
        let line = index + 1;
        let row = row.map_err(|_| LoadError::Io { line })?;
        let value: serde_json::Value =
            serde_json::from_str(&row).map_err(|_| LoadError::InvalidJson { line })?;
        if value["gold"].as_str().is_none_or(|s| s.trim().is_empty()) {
            return Err(LoadError::MissingGold { line });
        }
        if let Some(kind) = value["kind"].as_str() {
            if !matches!(kind, "rule" | "knowledge") {
                return Err(LoadError::UnknownKind {
                    line,
                    kind: kind.into(),
                });
            }
        }
        let probe: Probe =
            serde_json::from_value(value).map_err(|_| LoadError::InvalidJson { line })?;
        if !ids.insert(probe.id.clone()) {
            return Err(LoadError::DuplicateId { line, id: probe.id });
        }
        probes.push(probe);
    }
    Ok(probes)
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ToolCall {
    pub tool: String,
    pub file_class: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct RunRecord {
    pub final_answer: String,
    pub cited_ids: Vec<String>,
    pub tool_calls: Vec<ToolCall>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SourceStatus {
    InForce,
    Superseded,
    History,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct RunConfig {
    pub leg: String,
    pub fresh_session: bool,
    pub tool_set: Vec<String>,
    pub repo_sha: String,
    pub no_memory_dir: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct Score {
    pub probe_id: String,
    pub answer_accuracy: f64,
    pub source_currency: f64,
    pub retrieval_path: f64,
    pub reason: String,
    pub config: RunConfig,
}

pub fn score(
    probe: &Probe,
    run: &RunRecord,
    in_force: &BTreeMap<String, SourceStatus>,
    config: &RunConfig,
) -> Score {
    let gold = tokens(&probe.gold);
    let answer = tokens(&run.final_answer);
    let correct = !gold.is_empty() && answer.windows(gold.len()).any(|w| w == gold);
    let current = !run.cited_ids.is_empty()
        && run
            .cited_ids
            .iter()
            .all(|id| in_force.get(id) == Some(&SourceStatus::InForce));
    let path = run
        .tool_calls
        .iter()
        .any(|call| match &probe.expected_path {
            RetrievalPath::Tool(tool) => &call.tool == tool,
            RetrievalPath::FileClass(class) => call.file_class.as_ref() == Some(class),
        });
    Score {
        probe_id: probe.id.clone(),
        answer_accuracy: f64::from(correct),
        source_currency: f64::from(current),
        retrieval_path: f64::from(path),
        reason: [
            if correct {
                "answer_match"
            } else {
                "answer_mismatch"
            },
            if current {
                "all_citations_in_force"
            } else {
                "citation_missing_or_not_in_force"
            },
            if path { "path_match" } else { "path_mismatch" },
        ]
        .join("; "),
        config: config.clone(),
    }
}

fn tokens(text: &str) -> Vec<String> {
    text.split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .map(str::to_lowercase)
        .collect()
}

#[cfg(test)]
mod tests;
