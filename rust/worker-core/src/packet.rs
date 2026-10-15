//! Park packet: what a human or planner needs to pick a stuck card back up.

use crate::config::Tier;
use crate::exit_class::ExitClass;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ParkPacket {
    pub card: String,
    pub tier: Tier,
    pub exit_class: ExitClass,
    pub check_tail: String,
}

impl ParkPacket {
    pub fn to_json(&self) -> String {
        serde_json::to_string(self).expect("packet serializes")
    }

    pub fn from_json(s: &str) -> Result<Self, serde_json::Error> {
        serde_json::from_str(s)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_shape_and_roundtrip() {
        let p = ParkPacket {
            card: "c-1".into(),
            tier: Tier::Free,
            exit_class: ExitClass::CheckFail,
            check_tail: "line1\nline2".into(),
        };
        let v: serde_json::Value = serde_json::from_str(&p.to_json()).unwrap();
        assert_eq!(v["card"], "c-1");
        assert_eq!(v["tier"], "free");
        assert_eq!(v["exit_class"], "check_fail");
        assert_eq!(v["check_tail"], "line1\nline2");
        assert_eq!(v.as_object().unwrap().len(), 4);
        assert_eq!(ParkPacket::from_json(&p.to_json()).unwrap(), p);
        assert!(ParkPacket::from_json("{}").is_err());
    }
}
