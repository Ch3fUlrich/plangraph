//! The phase workflow's wire types and pure helpers.

use crate::attempt::LegSpec;
use serde::{Deserialize, Serialize};

pub const DEFAULT_MAX_PARALLEL: u32 = 3;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PhaseInput {
    #[serde(default)]
    pub max_waves: Option<u32>,
    #[serde(default)]
    pub finish_poll_s: Option<u64>,
    #[serde(default)]
    pub finish_max_polls: Option<u32>,
    pub key: String,
    pub repo: String,
    pub workroot: String,
    pub base_branch: String,
    #[serde(default)]
    pub max_parallel: Option<u32>,
    pub legs: Vec<LegSpec>,
}

impl PhaseInput {
    pub fn parallel(&self) -> usize {
        self.max_parallel
            .unwrap_or(DEFAULT_MAX_PARALLEL)
            .clamp(1, 16) as usize
    }

    pub fn finish_poll(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.finish_poll_s.unwrap_or(30).clamp(1, 600))
    }

    pub fn finish_polls(&self) -> u32 {
        self.finish_max_polls.unwrap_or(40).clamp(1, 500)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PhasePr {
    pub number: u64,
    #[serde(default)]
    pub url: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PhaseFinish {
    pub state: String,
    #[serde(default)]
    pub pr: Option<PhasePr>,
    #[serde(default)]
    pub detail: String,
}

pub const FINISH_MERGED: &str = "merged";
pub const FINISH_WAITING_CI: &str = "waiting_ci";
// refused = the keeper rejected the call; detail says why
pub const FINISH_REFUSED: &str = "refused";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlanRoute {
    #[serde(default)]
    pub window_class: String,
    #[serde(default)]
    pub harness: String,
    #[serde(default)]
    pub legs: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlanCard {
    pub key: String,
    #[serde(default)]
    pub ready: bool,
    #[serde(default)]
    pub deferred: bool,
    #[serde(default)]
    pub write_set: Vec<String>,
    #[serde(default)]
    pub route: Option<PlanRoute>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PhasePlan {
    #[serde(default)]
    pub phase: String,
    #[serde(default)]
    pub ready: bool,
    #[serde(default)]
    pub blocked: Vec<String>,
    #[serde(default)]
    pub cards: Vec<PlanCard>,
}

impl PhasePlan {
    pub fn startable(&self) -> Vec<&PlanCard> {
        if !self.ready || !self.blocked.is_empty() {
            return Vec::new();
        }

        let mut cards: Vec<&PlanCard> = self
            .cards
            .iter()
            .filter(|card| card.ready && !card.deferred && card.route.is_some())
            .collect();
        cards.sort_by(|a, b| a.key.as_str().cmp(b.key.as_str()));
        cards
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PhaseOutput {
    pub phase: String,
    pub branch: String,
    pub outcome: String,
    #[serde(default)]
    pub blocked: Vec<String>,
    #[serde(default)]
    pub merged: Vec<String>,
    #[serde(default)]
    pub not_merged: Vec<String>,
    pub waves: u32,
    #[serde(default)]
    pub finish: Option<PhaseFinish>,
}

pub const OUTCOME_BLOCKED: &str = "blocked";
pub const OUTCOME_DONE: &str = "done";
pub const OUTCOME_PARTIAL: &str = "partial";

pub fn phase_branch(key: &str) -> Result<String, String> {
    if key.is_empty() {
        return Err("phase key is empty".to_string());
    }

    let safe: String = key
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-') {
                ch
            } else {
                '-'
            }
        })
        .collect();

    if safe.is_empty() {
        return Err("phase key sanitizes to empty".to_string());
    }
    if safe.starts_with('.') {
        return Err("phase key must not start with '.'".to_string());
    }
    if safe.ends_with('.') {
        return Err("phase key must not end with '.'".to_string());
    }
    if safe.ends_with(".lock") {
        return Err("phase key must not end with '.lock'".to_string());
    }
    if safe.contains("..") {
        return Err("phase key must not contain '..'".to_string());
    }
    if safe.len() > 120 {
        return Err("phase key is longer than 120 characters".to_string());
    }

    Ok(format!("phase/{safe}"))
}

pub fn card_workflow_id(phase_key: &str, card_key: &str, n: u32) -> String {
    format!("card:{phase_key}:{card_key}:{n}")
}

pub fn select_legs(catalog: &[LegSpec], ids: &[String]) -> Vec<LegSpec> {
    let mut seen = std::collections::HashSet::new();
    let mut selected = Vec::new();

    for id in ids {
        if !seen.insert(id.as_str()) {
            continue;
        }
        if let Some(leg) = catalog
            .iter()
            .find(|candidate| candidate.id.as_str() == id.as_str())
        {
            selected.push(leg.clone());
        }
    }

    selected
}

pub fn next_wave<'a>(
    plan: &'a PhasePlan,
    attempted: &std::collections::HashSet<String>,
    limit: usize,
) -> Vec<&'a PlanCard> {
    let mut wave = Vec::new();
    for card in plan.startable() {
        if attempted.contains(&card.key) {
            continue;
        }
        if wave.len() >= limit {
            break;
        }
        wave.push(card);
    }
    wave
}

pub const DEFAULT_MAX_WAVES: u32 = 64;

impl PhaseInput {
    pub fn wave_cap(&self) -> u32 {
        self.max_waves.unwrap_or(DEFAULT_MAX_WAVES).clamp(1, 256)
    }
}

impl PhasePlan {
    pub fn unstarted(&self, attempted: &std::collections::HashSet<String>) -> Vec<String> {
        let mut keys: Vec<String> = self
            .cards
            .iter()
            .filter(|c| !attempted.contains(&c.key))
            .map(|c| c.key.clone())
            .collect();
        keys.sort();
        keys
    }
}

/// ready=false with no blockers means nothing is startable (e.g. every card
/// claimed), not a stop: PhasePlan::startable already yields no wave then, so
/// the loop ends via the empty wave and the finish step decides.
pub fn replan_stop(plan: &PhasePlan, key: &str) -> Option<Vec<String>> {
    if plan.phase != key {
        Some(vec!["plan is for another phase".to_string()])
    } else if !plan.blocked.is_empty() {
        Some(plan.blocked.clone())
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replan_stop_other_phase_reports_another_phase() {
        let plan = PhasePlan {
            phase: "PH-A".to_string(),
            ready: true,
            blocked: Vec::new(),
            cards: Vec::new(),
        };
        assert_eq!(
            replan_stop(&plan, "PH-B"),
            Some(vec!["plan is for another phase".to_string()])
        );
    }

    #[test]
    fn replan_stop_reports_blocked_cards() {
        let plan = PhasePlan {
            phase: "PH-A".to_string(),
            ready: true,
            blocked: vec!["card blocked".to_string()],
            cards: Vec::new(),
        };
        assert_eq!(
            replan_stop(&plan, "PH-A"),
            Some(vec!["card blocked".to_string()])
        );
    }

    #[test]
    fn replan_stop_ready_false_without_blockers_is_not_a_stop() {
        let plan = PhasePlan {
            phase: "PH-A".to_string(),
            ready: false,
            blocked: Vec::new(),
            cards: Vec::new(),
        };
        assert_eq!(replan_stop(&plan, "PH-A"), None);
    }

    #[test]
    fn replan_stop_ready_clean_phase_is_not_a_stop() {
        let plan = PhasePlan {
            phase: "PH-A".to_string(),
            ready: true,
            blocked: Vec::new(),
            cards: Vec::new(),
        };
        assert_eq!(replan_stop(&plan, "PH-A"), None);
    }

    #[test]
    fn unstarted_reports_cards_not_attempted() {
        let plan = PhasePlan {
            phase: "PH-X".to_string(),
            ready: true,
            blocked: Vec::new(),
            cards: vec![
                card("b", true, false),
                card("a", false, false),
                card("c", true, true),
            ],
        };
        let attempted: std::collections::HashSet<String> =
            vec!["a".to_string()].into_iter().collect();
        assert_eq!(
            plan.unstarted(&attempted),
            vec!["b".to_string(), "c".to_string()]
        );
    }

    #[test]
    fn wave_cap_defaults_and_clamps() {
        let mut phase = input(None);
        assert_eq!(phase.wave_cap(), DEFAULT_MAX_WAVES);
        phase.max_waves = Some(0);
        assert_eq!(phase.wave_cap(), 1);
        phase.max_waves = Some(1000);
        assert_eq!(phase.wave_cap(), 256);
        phase.max_waves = Some(5);
        assert_eq!(phase.wave_cap(), 5);
    }

    #[test]
    fn finish_poll_defaults_and_clamps() {
        let mut phase = input(None);
        assert_eq!(phase.finish_poll(), std::time::Duration::from_secs(30));
        phase.finish_poll_s = Some(0);
        assert_eq!(phase.finish_poll(), std::time::Duration::from_secs(1));
        phase.finish_poll_s = Some(1000);
        assert_eq!(phase.finish_poll(), std::time::Duration::from_secs(600));
        phase.finish_poll_s = Some(45);
        assert_eq!(phase.finish_poll(), std::time::Duration::from_secs(45));

        assert_eq!(phase.finish_polls(), 40);
        phase.finish_max_polls = Some(0);
        assert_eq!(phase.finish_polls(), 1);
        phase.finish_max_polls = Some(1000);
        assert_eq!(phase.finish_polls(), 500);
        phase.finish_max_polls = Some(3);
        assert_eq!(phase.finish_polls(), 3);
    }
    use pg_worker_core::config::Tier;
    use std::collections::HashSet;

    fn leg(id: &str) -> LegSpec {
        LegSpec {
            id: id.to_string(),
            tier: Tier::Free,
            model: "model".to_string(),
            harness: "harness".to_string(),
        }
    }

    fn card(key: &str, ready: bool, deferred: bool) -> PlanCard {
        PlanCard {
            key: key.to_string(),
            ready,
            deferred,
            write_set: Vec::new(),
            route: Some(PlanRoute {
                window_class: "w".to_string(),
                harness: "h".to_string(),
                legs: Vec::new(),
            }),
        }
    }

    fn input(max_parallel: Option<u32>) -> PhaseInput {
        PhaseInput {
            max_waves: None,
            finish_poll_s: None,
            finish_max_polls: None,
            key: "PH-X".to_string(),
            repo: "repo".to_string(),
            workroot: "/tmp/work".to_string(),
            base_branch: "main".to_string(),
            max_parallel,
            legs: vec![leg("l1")],
        }
    }

    #[test]
    fn startable_from_realistic_plan_json() {
        let plan_json = r#"{
            "phase": "PH-X",
            "ready": true,
            "blocked": [],
            "start_gate": {"ok": true, "extra": [1, 2, 3]},
            "cards": [
                {
                    "key": "b",
                    "ready": true,
                    "deferred": false,
                    "write_set": ["src/b.rs"],
                    "route": {"window_class": "small", "harness": "local", "legs": ["l1"], "review_seats": 2},
                    "findings": [],
                    "shadow_id": null,
                    "unknown_card_field": "ignored"
                },
                {
                    "key": "a",
                    "ready": true,
                    "deferred": false,
                    "write_set": [],
                    "route": {"window_class": "small", "harness": "local", "legs": [], "review_seats": 1},
                    "route_error": null
                },
                {
                    "key": "deferred",
                    "ready": true,
                    "deferred": true,
                    "write_set": [],
                    "route": {"window_class": "small", "harness": "local", "legs": []}
                },
                {
                    "key": "null_route",
                    "ready": true,
                    "deferred": false,
                    "write_set": [],
                    "route": null
                },
                {
                    "key": "not_ready",
                    "ready": false,
                    "deferred": false,
                    "write_set": [],
                    "route": {"window_class": "small", "harness": "local", "legs": []}
                }
            ],
            "overlaps": [],
            "skipped": [],
            "busy": []
        }"#;

        let plan: PhasePlan = serde_json::from_str(plan_json).unwrap();
        let keys: Vec<&str> = plan
            .startable()
            .into_iter()
            .map(|card| card.key.as_str())
            .collect();

        assert_eq!(keys, vec!["a", "b"]);
    }

    #[test]
    fn startable_is_empty_when_blocked_or_not_ready() {
        let blocked = PhasePlan {
            phase: "PH-X".to_string(),
            ready: true,
            blocked: vec!["gate".to_string()],
            cards: vec![card("a", true, false)],
        };
        assert!(blocked.startable().is_empty());

        let not_ready = PhasePlan {
            phase: "PH-X".to_string(),
            ready: false,
            blocked: Vec::new(),
            cards: vec![card("a", true, false)],
        };
        assert!(not_ready.startable().is_empty());
    }

    #[test]
    fn phase_branch_sanitizes_and_rejects_invalid_keys() {
        assert_eq!(phase_branch("demo:PH-X").unwrap(), "phase/demo-PH-X");
        assert_eq!(phase_branch("a b/c").unwrap(), "phase/a-b-c");
        assert_eq!(phase_branch("a_b.c-1").unwrap(), "phase/a_b.c-1");
        assert_eq!(
            phase_branch(&"a".repeat(120)).unwrap(),
            format!("phase/{}", "a".repeat(120))
        );

        assert!(phase_branch("").is_err());
        assert!(phase_branch(".hidden").is_err());
        assert!(phase_branch("trailing.").is_err());
        assert!(phase_branch("two..dots").is_err());
        assert!(phase_branch("branch.lock").is_err());
        assert!(phase_branch(&"a".repeat(121)).is_err());
    }

    #[test]
    fn card_workflow_id_formats_key_and_index() {
        assert_eq!(card_workflow_id("PH-X", "demo:K", 2), "card:PH-X:demo:K:2");
    }

    #[test]
    fn card_workflow_id_differs_per_phase() {
        assert_ne!(
            card_workflow_id("PH-X", "demo:K", 1),
            card_workflow_id("PH-Y", "demo:K", 1)
        );
    }

    #[test]
    fn select_legs_follows_ids_and_dedupes() {
        let catalog = vec![leg("a"), leg("b"), leg("c")];
        let ids = vec![
            "c".to_string(),
            "missing".to_string(),
            "a".to_string(),
            "c".to_string(),
            "b".to_string(),
        ];

        assert_eq!(
            select_legs(&catalog, &ids),
            vec![leg("c"), leg("a"), leg("b")]
        );
    }

    #[test]
    fn next_wave_respects_attempted_and_limit() {
        let plan = PhasePlan {
            phase: "PH-X".to_string(),
            ready: true,
            blocked: Vec::new(),
            cards: vec![
                card("a", true, false),
                card("b", true, false),
                card("c", true, false),
            ],
        };
        let mut attempted = HashSet::new();
        attempted.insert("b".to_string());

        let keys: Vec<&str> = next_wave(&plan, &attempted, 1)
            .into_iter()
            .map(|card| card.key.as_str())
            .collect();
        assert_eq!(keys, vec!["a"]);

        let keys: Vec<&str> = next_wave(&plan, &attempted, 10)
            .into_iter()
            .map(|card| card.key.as_str())
            .collect();
        assert_eq!(keys, vec!["a", "c"]);

        assert!(next_wave(&plan, &attempted, 0).is_empty());
    }

    #[test]
    fn parallel_clamps_to_supported_range() {
        assert_eq!(input(None).parallel(), 3);
        assert_eq!(input(Some(0)).parallel(), 1);
        assert_eq!(input(Some(1)).parallel(), 1);
        assert_eq!(input(Some(8)).parallel(), 8);
        assert_eq!(input(Some(16)).parallel(), 16);
        assert_eq!(input(Some(99)).parallel(), 16);
    }

    #[test]
    fn phase_output_round_trips_through_serde_json() {
        let output = PhaseOutput {
            phase: "PH-X".to_string(),
            branch: "phase/demo-PH-X".to_string(),
            outcome: OUTCOME_PARTIAL.to_string(),
            blocked: vec!["b".to_string()],
            merged: vec!["a".to_string()],
            not_merged: vec!["c".to_string()],
            waves: 2,
            finish: Some(PhaseFinish {
                state: FINISH_MERGED.to_string(),
                pr: Some(PhasePr {
                    number: 7,
                    url: "u".to_string(),
                }),
                detail: String::new(),
            }),
        };

        let json = serde_json::to_string(&output).unwrap();
        let back: PhaseOutput = serde_json::from_str(&json).unwrap();
        assert_eq!(back, output);
    }
}
