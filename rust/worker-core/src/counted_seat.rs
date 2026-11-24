//! Routing's persisted canary and the exact review evidence that it authorizes.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

const MIN_SAMPLE: usize = 10;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Revision {
    pub revision: String,
    pub leg: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Selection {
    pub seat: Revision,
    pub lane: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub selection: Selection,
    pub head: String,
    pub run_id: String,
    pub item_key: Option<String>,
    pub diff_lines: Option<u64>,
    pub writer_families: BTreeSet<String>,
}

#[derive(Debug, Deserialize)]
struct Sample {
    binding: Binding,
    accepted: bool,
    round: Option<u64>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Action {
    Promote,
    Rollback,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Decision {
    revision: String,
    action: Action,
    #[serde(default)]
    samples: Vec<Sample>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    previous: Revision,
    candidate: Revision,
    cohort: [String; 3],
    heads: BTreeMap<String, String>,
    decision: Option<Decision>,
}

fn token(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-' | b'/'))
}

pub fn sha(s: &str) -> bool {
    s.len() == 40
        && s.bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

impl Policy {
    pub fn read(path: &Path) -> Result<Option<Self>, String> {
        let data = std::fs::read(path).map_err(|_| "cannot read route policy")?;
        let document: Value =
            serde_json::from_slice(&data).map_err(|_| "invalid route policy JSON")?;
        let Some(value) = document.get("counted_review") else {
            return Ok(None);
        };
        let policy: Self =
            serde_json::from_value(value.clone()).map_err(|_| "invalid counted review policy")?;
        if policy.previous.revision == policy.candidate.revision
            || policy.previous.leg == policy.candidate.leg
            || [&policy.previous, &policy.candidate]
                .iter()
                .any(|r| !token(&r.revision) || !token(&r.leg))
            || BTreeSet::from(policy.cohort.each_ref()).len() != 3
            || policy.cohort.iter().any(|lane| !token(lane))
            || policy
                .heads
                .iter()
                .any(|(head, lane)| !sha(head) || !token(lane))
            || policy
                .decision
                .as_ref()
                .is_some_and(|d| d.revision != policy.candidate.revision)
        {
            return Err("invalid counted review revisions or lane assignments".into());
        }
        Ok(Some(policy))
    }

    pub fn configured() -> Result<Option<Self>, String> {
        std::env::var_os("PLANGRAPH_ROUTE_POLICY")
            .filter(|p| !p.is_empty())
            .map_or(Ok(None), |path| Self::read(Path::new(&path)))
    }

    pub fn select(&self, head: &str) -> Selection {
        let lane = self.heads.get(head);
        let use_candidate = match &self.decision {
            Some(Decision {
                action: Action::Rollback,
                ..
            }) => false,
            Some(Decision {
                action: Action::Promote,
                samples,
                ..
            }) if self.comparable(samples) => true,
            _ => lane.is_some_and(|lane| self.cohort.contains(lane)),
        };
        Selection {
            seat: if use_candidate {
                &self.candidate
            } else {
                &self.previous
            }
            .clone(),
            lane: lane.cloned(),
        }
    }

    pub fn verify(
        &self,
        binding: &Binding,
        head: &str,
        run_id: &str,
        served: &str,
    ) -> Result<(), String> {
        let selected = self.select(head);
        if selected.lane.is_none()
            || binding.selection != selected
            || binding.head != head
            || binding.run_id != run_id
            || served != selected.seat.leg
        {
            return Err(
                "counted review does not match the lane, revision, head, run and serving leg"
                    .into(),
            );
        }
        Ok(())
    }

    fn comparable(&self, samples: &[Sample]) -> bool {
        type Stratum<'a> = (&'a BTreeSet<String>, u64);
        let mut pairs: BTreeMap<Stratum<'_>, [BTreeMap<&str, &Sample>; 2]> = BTreeMap::new();
        let mut unique = BTreeSet::new();
        for sample in samples {
            let binding = &sample.binding;
            let Some(lane) = self.heads.get(&binding.head) else {
                return false;
            };
            let candidate = self.cohort.contains(lane);
            let revision = if candidate {
                &self.candidate
            } else {
                &self.previous
            };
            let Some(lines) = binding.diff_lines else {
                continue;
            };
            if !unique.insert(binding.head.as_str())
                || binding.selection.lane.as_ref() != Some(lane)
                || &binding.selection.seat != revision
                || binding.writer_families.is_empty()
                || !crate::run::record::valid_run_id(&binding.run_id)
                || sample.accepted && !sample.round.is_some_and(|n| n > 0)
            {
                return false;
            }
            // 1–99, 100–199, and larger size bands are compared within an identical
            // writer-family set. Equal sample sizes give each band the same weight.
            if pairs
                .entry((&binding.writer_families, lines / 100))
                .or_default()[usize::from(candidate)]
            .insert(&binding.head, sample)
            .is_some()
            {
                return false;
            }
        }
        let mut matched = [Vec::new(), Vec::new()];
        for [old, new] in pairs.into_values() {
            let n = old.len().min(new.len());
            matched[0].extend(old.into_values().take(n));
            matched[1].extend(new.into_values().take(n));
        }
        let n = matched[0].len();
        if n < MIN_SAMPLE {
            return false;
        }
        let mut rounds = matched.map(|seats| {
            let mut passed: Vec<_> = seats
                .into_iter()
                .filter(|s| s.accepted)
                .filter_map(|s| s.round)
                .collect();
            passed.sort_unstable();
            passed
        });
        let [old, new] = &mut rounds;
        let rate_ok = new.len() * 100 >= n * 15 && new.len() * 100 + n * 5 >= old.len() * 100;
        let median = |values: &[u64]| -> f64 {
            let n = values.len();
            (values[(n - 1) / 2] as f64 + values[n / 2] as f64) / 2.0
        };
        rate_ok && !old.is_empty() && median(new) <= median(old) + 1.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn promotion_uses_matched_head_sizes_and_known_rounds() {
        let previous = Revision {
            revision: "v1".into(),
            leg: "seat-old".into(),
        };
        let candidate = Revision {
            revision: "v2".into(),
            leg: "seat-new".into(),
        };
        let mut policy = Policy {
            previous: previous.clone(),
            candidate: candidate.clone(),
            cohort: ["lane-a".into(), "lane-b".into(), "lane-c".into()],
            heads: BTreeMap::new(),
            decision: None,
        };
        let mut samples = Vec::new();
        for index in 0..20 {
            let new = index >= 10;
            let head = format!("{index:040x}");
            let lane = if new { "lane-a" } else { "lane-d" }.to_owned();
            policy.heads.insert(head.clone(), lane.clone());
            samples.push(Sample {
                binding: Binding {
                    selection: Selection {
                        seat: if new {
                            candidate.clone()
                        } else {
                            previous.clone()
                        },
                        lane: Some(lane),
                    },
                    head,
                    run_id: format!("run-{index:032x}"),
                    item_key: None,
                    diff_lines: Some(if new { 230 } else { 130 }),
                    writer_families: BTreeSet::from(["openai".into()]),
                },
                accepted: true,
                round: Some(if new { 3 } else { 1 }),
            });
        }
        assert!(!policy.comparable(&samples));
        for sample in &mut samples[10..] {
            sample.binding.diff_lines = Some(190);
        }
        assert!(!policy.comparable(&samples));
        for sample in &mut samples[10..] {
            sample.round = Some(2);
        }
        assert!(policy.comparable(&samples));
        let fourth = samples[0].binding.clone();
        assert!(policy
            .verify(&fourth, &fourth.head, &fourth.run_id, "seat-old")
            .is_ok());
        policy.decision = Some(Decision {
            revision: "v2".into(),
            action: Action::Promote,
            samples,
        });
        assert_eq!(policy.select(&fourth.head).seat, candidate);
        assert!(policy
            .verify(&fourth, &fourth.head, &fourth.run_id, "seat-old")
            .is_err());
        policy.decision = Some(Decision {
            revision: "v2".into(),
            action: Action::Rollback,
            samples: vec![],
        });
        assert!(policy
            .verify(&fourth, &fourth.head, &fourth.run_id, "seat-old")
            .is_ok());
    }
}
