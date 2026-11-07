//! Card wire input and compatibility defaults.
use serde::{Deserialize, Serialize};

/// Workflow wire input. Kept here so gate metadata does not change the legacy
/// activity input contract; missing fields preserve recorded start payloads.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CardInput {
    #[serde(flatten)]
    pub legacy: crate::activities::CardInput,
    #[serde(default)]
    pub risk_tagged: bool,
    #[serde(default)]
    pub heldout_cmd: String,
    #[serde(default)]
    pub smoke_checks: Vec<String>,
    #[serde(default)]
    pub exports: Vec<String>,
    #[serde(default)]
    pub base_sha: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resume: Option<crate::rollover::Resume>,
}
impl std::ops::Deref for CardInput {
    type Target = crate::activities::CardInput;

    fn deref(&self) -> &Self::Target {
        &self.legacy
    }
}

impl std::ops::DerefMut for CardInput {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.legacy
    }
}
