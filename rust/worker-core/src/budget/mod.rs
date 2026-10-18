//! Retry budget: a pure policy from attempt history to the next action.
//!
//! Randomness (jitter) and the clock are inputs, so every rule is testable.
//! Policy order, after looking at the latest attempt:
//!
//! 1. `ok` -> `Done`; `contract_defect` -> `ToPlanner`; `probe_fail` -> `Backoff`;
//!    `cancelled` / `needs_input` -> `Park`; `flaky_check` -> `Quarantine`.
//! 2. `usage_limit` / `outage` -> `Reroute` to the next unlimited leg of the same
//!    tier; failing that, to an unlimited leg of a *different harness* in any tier
//!    (lowest tier first); else `Wait` (exponential, jittered, capped). These
//!    never consume attempts.
//! 3. Context overflow -> `UpgradeClass` (128k -> 256k -> 1m), then `Park`.
//! 4. Two identical failures (`check_fail` / `no_change`) with an unchanged
//!    diff -> `Quarantine`.
//! 5. Fewer than `retries_per_tier` attempts on the tier -> `Retry`; otherwise
//!    `Promote` to the next tier (once per card, from a clean worktree) or `Park`.

use crate::config::{Config, Tier};
use crate::exit_class::ExitClass;
use crate::packet::ParkPacket;
use serde::{Deserialize, Serialize};
use std::time::{Duration, SystemTime};

#[cfg(test)]
mod tests;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ContextClass {
    K128,
    K256,
    M1,
}

impl ContextClass {
    pub fn next(self) -> Option<ContextClass> {
        match self {
            ContextClass::K128 => Some(ContextClass::K256),
            ContextClass::K256 => Some(ContextClass::M1),
            ContextClass::M1 => None,
        }
    }
}

/// One finished attempt.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Attempt {
    pub exit_class: ExitClass,
    pub leg: String,
    pub tier: Tier,
    pub diff_hash: String,
    pub check_hash: String,
    pub context_overflow: bool,
}

impl Attempt {
    /// `usage_limit` and `outage` are the provider's fault and do not count.
    pub fn consumes_attempt(&self) -> bool {
        !matches!(self.exit_class, ExitClass::UsageLimit | ExitClass::Outage)
    }

    fn limited(&self) -> bool {
        !self.consumes_attempt()
    }

    /// A no-change run and a failed check are the same dead end: the work is
    /// unfinished, the provider is healthy, and the attempt consumed budget.
    fn same_failure_kind(&self, other: &Attempt) -> bool {
        matches!(
            (self.exit_class, other.exit_class),
            (
                ExitClass::CheckFail | ExitClass::NoChange,
                ExitClass::CheckFail | ExitClass::NoChange
            )
        )
    }
}

/// A provider leg available to the card.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Leg {
    pub id: String,
    pub tier: Tier,
    /// The agent harness / model family behind the leg (`opencode`, `claude`, ...).
    /// Only compared for equality: a limit on one family is rerouted to another.
    pub harness: String,
    /// Caller-tracked: currently rate-limited or down.
    pub limited: bool,
}

pub struct Input<'a> {
    pub card: &'a str,
    pub history: &'a [Attempt],
    pub legs: &'a [Leg],
    pub context: ContextClass,
    /// Consecutive waits already served since the last non-limited attempt.
    pub waits: u32,
    pub now: SystemTime,
    /// Uniform sample in [0, 1) for jitter; see [`random_jitter`].
    pub jitter: f64,
    pub check_tail: &'a str,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum Action {
    Retry,
    Done,
    Reroute { leg: String },
    Wait { until: SystemTime },
    Promote { tier: Tier },
    UpgradeClass { class: ContextClass },
    Quarantine,
    Park { packet: ParkPacket },
    ToPlanner,
    Backoff { delay: Duration },
}

pub fn random_jitter() -> f64 {
    rand::random::<f64>()
}

/// `base * 2^n`, capped, then "equal jitter": scaled into [50%, 100%].
fn exp_delay(base: Duration, n: u32, cap: Duration, jitter: f64) -> Duration {
    let raw = base.as_secs_f64() * 2f64.powi(n.min(40) as i32);
    let capped = raw.min(cap.as_secs_f64());
    Duration::from_secs_f64(capped * (0.5 + 0.5 * jitter.clamp(0.0, 1.0)))
}

pub fn next_action(cfg: &Config, inp: &Input) -> Action {
    let Some(last) = inp.history.last() else {
        return Action::Retry;
    };
    let park = || Action::Park {
        packet: ParkPacket {
            card: inp.card.to_string(),
            tier: last.tier,
            exit_class: last.exit_class,
            check_tail: inp.check_tail.to_string(),
        },
    };
    match last.exit_class {
        ExitClass::Ok => return Action::Done,
        ExitClass::ContractDefect => return Action::ToPlanner,
        ExitClass::FlakyCheck => return Action::Quarantine,
        ExitClass::Cancelled | ExitClass::NeedsInput => return park(),
        ExitClass::ProbeFail => {
            let n = inp
                .history
                .iter()
                .rev()
                .take_while(|a| a.exit_class == ExitClass::ProbeFail)
                .count() as u32;
            let delay = exp_delay(cfg.backoff_base, n - 1, cfg.wait_cap, inp.jitter);
            return Action::Backoff { delay };
        }
        ExitClass::UsageLimit | ExitClass::Outage => return reroute_or_wait(cfg, inp, last),
        _ => {}
    }
    if last.context_overflow {
        return match inp.context.next() {
            Some(class) => Action::UpgradeClass { class },
            None => park(),
        };
    }
    let counted: Vec<&Attempt> = inp
        .history
        .iter()
        .filter(|a| a.consumes_attempt())
        .collect();
    if let [.., a, b] = counted[..] {
        if a.same_failure_kind(b) && a.diff_hash == b.diff_hash && a.check_hash == b.check_hash {
            return Action::Quarantine;
        }
    }
    let used = counted.iter().filter(|a| a.tier == last.tier).count() as u32;
    if used < cfg.retries_per_tier {
        return Action::Retry;
    }
    let promoted = inp
        .history
        .windows(2)
        .filter(|w| w[1].tier > w[0].tier)
        .count() as u32;
    match last.tier.next() {
        Some(tier) if promoted < cfg.promotions => Action::Promote { tier },
        _ => park(),
    }
}

fn reroute_or_wait(cfg: &Config, inp: &Input, last: &Attempt) -> Action {
    let legs: Vec<&Leg> = inp.legs.iter().filter(|l| l.tier == last.tier).collect();
    let start = legs
        .iter()
        .position(|l| l.id == last.leg)
        .map_or(0, |p| p + 1);
    for i in 0..legs.len() {
        let leg = legs[(start + i) % legs.len()];
        // the leg that just failed counts as limited even if the caller has not flagged it
        let limited = leg.limited || (leg.id == last.leg && last.limited());
        if !limited {
            return Action::Reroute {
                leg: leg.id.clone(),
            };
        }
    }
    // Every leg of the tier is limited. No hard dependency on one provider:
    // a limit on one harness/family (say a Claude window) falls through to an
    // unlimited leg of another harness in ANY tier, lowest tier index first,
    // before the card sits in a Wait. Ties keep the caller's leg order. A
    // same-harness leg of another tier is not a fallback (it shares the limit),
    // so that case still waits.
    let failed_harness = inp
        .legs
        .iter()
        .find(|l| l.id == last.leg)
        .map(|l| l.harness.as_str());
    let fallback = inp
        .legs
        .iter()
        .filter(|l| !l.limited && Some(l.harness.as_str()) != failed_harness)
        .min_by_key(|l| l.tier);
    if let Some(leg) = fallback {
        return Action::Reroute {
            leg: leg.id.clone(),
        };
    }
    let delay = exp_delay(cfg.wait_base, inp.waits, cfg.wait_cap, inp.jitter);
    Action::Wait {
        until: inp.now + delay,
    }
}
