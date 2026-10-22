//! Phase workflow: plans a keeper gate, claims startable cards, and runs them
//! against a phase branch.

use std::collections::HashSet;
use std::time::Duration;

use futures::future::join_all;
use pg_worker_core::config::Config;
use temporalio_common::RetryPolicy;
use temporalio_macros::{workflow, workflow_methods};
use temporalio_sdk::{ActivityOptions, ChildWorkflowOptions, WorkflowContext, WorkflowResult};

use crate::activities::{
    CardActivities, CardInput, ClaimCardInput, PhaseBranchInput, PhaseBranchResult, PhasePlanInput,
};
use crate::keeper::Claim;
use crate::phase::{
    OUTCOME_BLOCKED, OUTCOME_DONE, OUTCOME_PARTIAL, PhaseInput, PhaseOutput, PhasePlan,
    card_workflow_id, next_wave, phase_branch, select_legs,
};
use crate::workflow::Card;

#[workflow]
#[derive(Default)]
pub struct Phase {}

#[workflow_methods]
impl Phase {
    #[run]
    pub async fn run(
        ctx: &mut WorkflowContext<Self>,
        input: PhaseInput,
    ) -> WorkflowResult<PhaseOutput> {
        let branch = match phase_branch(&input.key) {
            Ok(branch) => branch,
            Err(msg) => {
                return Ok(PhaseOutput {
                    phase: input.key,
                    branch: String::new(),
                    outcome: OUTCOME_BLOCKED.to_string(),
                    blocked: vec![msg],
                    merged: Vec::new(),
                    not_merged: Vec::new(),
                    waves: 0,
                });
            }
        };

        let cfg = Config::default();
        let mut attempted = HashSet::new();
        let mut merged = Vec::new();
        let mut not_merged = Vec::new();
        let mut waves = 0;
        let mut first_plan = true;
        let mut last_blocked: Vec<String> = Vec::new();

        loop {
            let plan: PhasePlan = ctx
                .execute_activity(
                    CardActivities::phase_plan,
                    PhasePlanInput {
                        key: input.key.clone(),
                    },
                    activity_options(&cfg),
                )
                .await?;

            if first_plan {
                first_plan = false;
                if plan.phase != input.key || !plan.ready || !plan.blocked.is_empty() {
                    let blocked = if plan.phase != input.key {
                        vec!["plan is for another phase".to_string()]
                    } else if plan.blocked.is_empty() {
                        vec!["phase not ready".to_string()]
                    } else {
                        plan.blocked.clone()
                    };
                    return Ok(PhaseOutput {
                        phase: input.key,
                        branch,
                        outcome: OUTCOME_BLOCKED.to_string(),
                        blocked,
                        merged: sorted_unique(merged),
                        not_merged: sorted_unique(not_merged),
                        waves,
                    });
                }
            } else {
                if plan.phase != input.key {
                    last_blocked = vec!["plan is for another phase".to_string()];
                    break;
                }
                if !plan.ready {
                    last_blocked = if plan.blocked.is_empty() {
                        vec!["phase not ready".to_string()]
                    } else {
                        plan.blocked.clone()
                    };
                    break;
                }
                if !plan.blocked.is_empty() {
                    last_blocked = plan.blocked.clone();
                    break;
                }
                last_blocked.clear();
            }

            let wave = next_wave(&plan, &attempted, input.parallel());
            if wave.is_empty() {
                break;
            }

            let head: PhaseBranchResult = match ctx
                .execute_activity(
                    CardActivities::ensure_phase_branch,
                    PhaseBranchInput {
                        repo: input.repo.clone(),
                        base_branch: input.base_branch.clone(),
                        branch: branch.clone(),
                    },
                    activity_options(&cfg),
                )
                .await
            {
                Ok(head) => head,
                Err(err) => {
                    return Ok(PhaseOutput {
                        phase: input.key,
                        branch,
                        outcome: OUTCOME_BLOCKED.to_string(),
                        blocked: vec![format!("ensure_phase_branch failed: {err}")],
                        merged: sorted_unique(merged),
                        not_merged: sorted_unique(not_merged),
                        waves,
                    });
                }
            };

            let mut started_children = Vec::new();
            for card in wave {
                attempted.insert(card.key.clone());

                let claim = match ctx
                    .execute_activity(
                        CardActivities::claim_card,
                        ClaimCardInput {
                            key: card.key.clone(),
                        },
                        activity_options(&cfg),
                    )
                    .await
                {
                    Ok(claim) => claim,
                    Err(_) => {
                        not_merged.push(card.key.clone());
                        continue;
                    }
                };

                let Some(claim) = claim else {
                    not_merged.push(card.key.clone());
                    continue;
                };

                let Some(route) = card.route.as_ref() else {
                    not_merged.push(card.key.clone());
                    continue;
                };

                let legs = select_legs(&input.legs, &route.legs);
                if legs.is_empty() {
                    not_merged.push(card.key.clone());
                    continue;
                }

                let claim = Claim {
                    base_sha: head.head.clone(),
                    repo: input.repo.clone(),
                    ..claim
                };

                let card_input = CardInput {
                    claim,
                    legs,
                    workroot: input.workroot.clone(),
                    main_branch: branch.clone(),
                    question_wait_cap_s: None,
                    max_question_rounds: None,
                };

                let child_id = card_workflow_id(card.key.as_str(), 1);
                match ctx
                    .start_child_workflow(
                        Card::run,
                        card_input,
                        ChildWorkflowOptions::workflow_id(child_id),
                    )
                    .await
                {
                    Ok(started) => started_children.push((card.key.clone(), started)),
                    Err(_) => not_merged.push(card.key.clone()),
                }
            }

            let results = join_all(
                started_children
                    .into_iter()
                    .map(|(key, started)| async move { (key, started.result().await) }),
            )
            .await;

            for (key, result) in results {
                match result {
                    Ok(output) if output.outcome == OUTCOME_DONE => merged.push(key),
                    _ => not_merged.push(key),
                }
            }
            waves += 1;
        }

        let merged = sorted_unique(merged);
        let not_merged = sorted_unique(not_merged);
        let outcome = phase_outcome(&not_merged, &last_blocked);

        Ok(PhaseOutput {
            phase: input.key,
            branch,
            outcome: outcome.to_string(),
            blocked: last_blocked,
            merged,
            not_merged,
            waves,
        })
    }
}

fn activity_options(cfg: &Config) -> ActivityOptions {
    let retry = RetryPolicy::builder()
        .initial_interval(Duration::from_secs(2))
        .maximum_attempts(5)
        .build();
    ActivityOptions::with_start_to_close_timeout(cfg.start_to_close)
        .retry_policy(retry)
        .build()
}

fn sorted_unique(mut keys: Vec<String>) -> Vec<String> {
    keys.sort();
    keys.dedup();
    keys
}

pub fn phase_outcome(not_merged: &[String], blocked: &[String]) -> &'static str {
    if not_merged.is_empty() && blocked.is_empty() {
        OUTCOME_DONE
    } else {
        OUTCOME_PARTIAL
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phase_outcome_done_when_nothing_ran() {
        assert_eq!(phase_outcome(&[], &[]), OUTCOME_DONE);
    }

    #[test]
    fn phase_outcome_partial_when_not_merged() {
        let not_merged = vec!["b".to_string()];
        assert_eq!(phase_outcome(&not_merged, &[]), OUTCOME_PARTIAL);
    }

    #[test]
    fn phase_outcome_partial_when_blocked_later() {
        let blocked = vec!["phase not ready".to_string()];
        assert_eq!(phase_outcome(&[], &blocked), OUTCOME_PARTIAL);
    }
}
