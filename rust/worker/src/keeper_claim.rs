use serde::{Deserialize, Serialize};

/// One unit of work handed to a worker.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Claim {
    pub card: String,
    pub repo: String,
    pub base_sha: String,
    pub check_cmd: String,
    /// `change` (the default) requires a red base; `regression` skips that gate.
    /// Recorded legacy claims may omit this or use "": both normalize to `change`.
    /// All other unknown values are refused; recorded history is never rewritten.
    #[serde(
        default = "default_check_kind",
        deserialize_with = "deserialize_check_kind"
    )]
    pub check_kind: String,
    /// Repo-relative check inputs, protected unless explicitly allowed.
    #[serde(default)]
    pub check_reads: Vec<String>,
    #[serde(default)]
    pub smoke_checks: Vec<String>,
    #[serde(default)]
    pub exports: Vec<String>,
    #[serde(default)]
    pub heldout_cmd: String,
    #[serde(default)]
    pub risk_tagged: bool,
    pub allow: Vec<String>,
    pub forbid: Vec<String>,
    pub prompt: String,
}
fn default_check_kind() -> String {
    "change".into()
}

fn deserialize_check_kind<'de, D: serde::Deserializer<'de>>(d: D) -> Result<String, D::Error> {
    let kind = String::deserialize(d)?;
    match kind.as_str() {
        "" => Ok(default_check_kind()),
        "change" | "regression" => Ok(kind),
        _ => Err(serde::de::Error::custom(
            "check_kind must be change or regression",
        )),
    }
}

impl Default for Claim {
    fn default() -> Self {
        Self {
            card: String::new(),
            repo: String::new(),
            base_sha: String::new(),
            check_cmd: String::new(),
            check_kind: default_check_kind(),
            check_reads: Vec::new(),
            smoke_checks: Vec::new(),
            exports: Vec::new(),
            heldout_cmd: String::new(),
            risk_tagged: false,
            allow: Vec::new(),
            forbid: Vec::new(),
            prompt: String::new(),
        }
    }
}
