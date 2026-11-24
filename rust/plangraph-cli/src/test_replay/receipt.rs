use super::super::Result;
use serde::{Deserialize, Serialize};
use std::{fmt::Write, path::Path};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(super) struct Observation {
    pub outcome: Outcome,
    pub summary: String,
    // A deliberately skipped or deselected pytest backend still records an inconclusive run.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub inactive: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub(super) enum Outcome {
    Passed,
    Failed,
    Inconclusive,
}

impl Observation {
    pub(super) fn inconclusive(summary: impl Into<String>) -> Self {
        Self {
            outcome: Outcome::Inconclusive,
            summary: summary.into(),
            inactive: false,
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub(super) struct TestResult {
    pub id: String,
    pub path: String,
    pub backend: String,
    pub base: Observation,
    pub head: Observation,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exception: Option<String>,
}

impl TestResult {
    pub(super) fn passed(&self) -> bool {
        self.head.outcome == Outcome::Passed
            && (self.base.outcome == Outcome::Failed || self.exception.is_some())
    }

    fn accepted(&self, all: &[Self]) -> bool {
        self.passed()
            || (self.head.inactive
                && self.base.outcome != Outcome::Passed
                && all.iter().any(|other| {
                    other.path == self.path
                        && other.backend != self.backend
                        && other.passed()
                        && (other.id == self.id
                            || other
                                .id
                                .split_once('[')
                                .is_some_and(|(function, _)| function == self.id))
                }))
    }

    fn line(&self) -> String {
        let show = |observation: &Observation| {
            let name = match observation.outcome {
                Outcome::Passed => "passed",
                Outcome::Failed => "failed",
                Outcome::Inconclusive => "inconclusive",
            };
            format!("{name} ({})", observation.summary.trim())
        };
        let exception = self.exception.as_ref().map_or(String::new(), |reason| {
            format!("; Test-Replay-Exception: {reason}")
        });
        format!(
            "{} [{}] base={}; head={}{}",
            self.id,
            self.backend,
            show(&self.base),
            show(&self.head),
            exception
        )
    }
}

/// Successful writer-host replay of changed tests and explicit committed-base claims.
#[derive(Debug, Serialize, Deserialize)]
pub struct ReplayReceipt {
    pub id: String,
    pub base: String,
    pub head: String,
    tests: Vec<TestResult>,
    committed_base_claims: Vec<(String, String)>,
}

impl ReplayReceipt {
    pub(super) fn new(
        base: &str,
        head: &str,
        tests: Vec<TestResult>,
        committed_base_claims: Vec<(String, String)>,
    ) -> Result<Self> {
        let mut id = [0u8; 16];
        getrandom::fill(&mut id).map_err(|error| error.to_string())?;
        let id = id.iter().fold(String::new(), |mut hex, byte| {
            let _ = write!(hex, "{byte:02x}");
            hex
        });
        let receipt = Self {
            id,
            base: base.into(),
            head: head.into(),
            tests,
            committed_base_claims,
        };
        let rejected: Vec<_> = receipt
            .tests
            .iter()
            .filter(|test| !test.accepted(&receipt.tests))
            .map(|test| {
                if test.base.outcome == Outcome::Passed {
                    format!("{} passed at base; {}", test.id, test.line())
                } else {
                    test.line()
                }
            })
            .collect();
        if rejected.is_empty() {
            Ok(receipt)
        } else {
            Err(format!(
                "changed-test replay refused for {base}..{head}\n{}\nFor a justified base exception, add Test-Replay-Exception: \"<test ID>\" <reason> to a commit containing the current test file. The head test still needs a test-body pass.",
                rejected.join("\n")
            ))
        }
    }

    pub fn matches(&self, base: &str, head: &str) -> bool {
        let commit = |sha: &str| {
            matches!(sha.len(), 40 | 64) && sha.bytes().all(|byte| byte.is_ascii_hexdigit())
        };
        commit(base)
            && commit(head)
            && self.base == base
            && self.head == head
            && self.id.len() == 32
            && self.id.bytes().all(|byte| byte.is_ascii_hexdigit())
            && self.tests.iter().all(|test| test.accepted(&self.tests))
    }

    pub fn write(&self, path: &Path) -> Result<()> {
        let bytes = serde_json::to_vec(self).map_err(|error| error.to_string())?;
        pg_worker_core::run::record_io::atomic_write_private(path, &bytes)
            .map_err(|error| error.to_string())
    }

    pub fn read(path: &Path) -> Result<Self> {
        let bytes = std::fs::read(path).map_err(|error| error.to_string())?;
        serde_json::from_slice(&bytes).map_err(|error| error.to_string())
    }

    pub fn evidence(&self) -> String {
        let mut text = format!(
            "Writer-host test replay (receipt {})\nBase: {}\nHead: {}\n",
            self.id, self.base, self.head
        );
        if self.tests.is_empty() {
            text.push_str("No changed test functions selected.\n");
        }
        for test in &self.tests {
            let _ = writeln!(text, "{}", test.line());
        }
        for (node, summary) in &self.committed_base_claims {
            let _ = writeln!(text, "Fails-at-Base {} {node}: {summary}", self.base);
        }
        text
    }
}
