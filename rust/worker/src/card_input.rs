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

/// Identity in the scheduled history, before any run-carrying worker verb.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProjectorInput {
    #[serde(flatten)]
    pub legacy: crate::attempt::AttemptInput,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workflow_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attempt: Option<u32>,
}

impl std::ops::Deref for ProjectorInput {
    type Target = crate::attempt::AttemptInput;
    fn deref(&self) -> &Self::Target {
        &self.legacy
    }
}

pub(crate) fn projector_input(
    ctx: &temporalio_sdk::WorkflowContext<crate::workflow::Card>,
    legacy: crate::attempt::AttemptInput,
) -> ProjectorInput {
    let enabled = ctx.patched("projector-run-ids-v1");
    ProjectorInput {
        workflow_id: enabled.then(|| ctx.workflow_id().to_owned()),
        run_id: enabled.then(|| ctx.run_id().to_owned()),
        attempt: enabled.then_some(legacy.n),
        legacy,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn projector_input_roundtrips_identity_and_legacy_history() {
        let json = serde_json::json!({"claim": {"card":"demo:A", "repo":"repo",
            "base_sha":"abc", "check_cmd":"true", "allow":[], "forbid":[], "prompt":"work"},
            "n":3, "leg":{"id":"leg", "tier":"free", "model":"model"},
            "workroot":"work", "class":"128k"});
        let mut input: ProjectorInput = serde_json::from_value(json.clone()).unwrap();
        assert_eq!(input.workflow_id, None);
        assert_eq!(input.run_id, None);
        assert_eq!(input.attempt, None);
        input.workflow_id = Some("card:demo:A".into());
        input.run_id = Some("R2".into());
        input.attempt = Some(input.n);
        let value = serde_json::to_value(&input).unwrap();
        assert_eq!(value["workflow_id"], "card:demo:A");
        assert_eq!(value["run_id"], "R2");
        assert_eq!(value["attempt"], 3);
        assert_eq!(
            serde_json::from_value::<ProjectorInput>(value).unwrap(),
            input
        );
    }
}
