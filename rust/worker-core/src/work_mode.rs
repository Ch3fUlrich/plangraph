//! Work-mode setting shared by orchestration consumers.

use serde::{Deserialize, Serialize};
use std::str::FromStr;

/// Requested orchestration mode. Auto selection is implemented by consumers.
#[non_exhaustive]
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", try_from = "String")]
pub enum WorkMode {
    Auto,
    Piecewise,
    #[default]
    Batch,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("unknown work mode {0:?}; expected auto, piecewise, batch")]
pub struct ParseWorkModeError(String);

impl FromStr for WorkMode {
    type Err = ParseWorkModeError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "auto" => Ok(Self::Auto),
            "piecewise" => Ok(Self::Piecewise),
            "batch" => Ok(Self::Batch),
            _ => Err(ParseWorkModeError(value.to_owned())),
        }
    }
}

// Require the schema's string representation; a derived enum deserializer alone
// also accepts externally tagged objects such as {"auto": null}.
impl TryFrom<String> for WorkMode {
    type Error = ParseWorkModeError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        value.parse()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_and_round_trips_all_modes() {
        for (name, mode) in [
            ("auto", WorkMode::Auto),
            ("piecewise", WorkMode::Piecewise),
            ("batch", WorkMode::Batch),
        ] {
            assert_eq!(name.parse::<WorkMode>().unwrap(), mode);
            assert_eq!(serde_json::to_value(mode).unwrap(), name);
            assert_eq!(
                serde_json::from_value::<WorkMode>(name.into()).unwrap(),
                mode
            );
        }
        assert_eq!(WorkMode::default(), WorkMode::Batch);
    }

    #[test]
    fn refuses_unknown_or_non_string_modes() {
        for name in ["", "AUTO", "batch ", "future"] {
            let error = name.parse::<WorkMode>().unwrap_err().to_string();
            assert!(error.contains("work mode"), "{error}");
            assert!(error.contains("auto, piecewise, batch"), "{error}");
            assert!(serde_json::from_value::<WorkMode>(name.into()).is_err());
        }
        for json in ["null", "42", "true", "[]", "{}", r#"{"auto":null}"#] {
            assert!(serde_json::from_str::<WorkMode>(json).is_err());
        }
    }
}
