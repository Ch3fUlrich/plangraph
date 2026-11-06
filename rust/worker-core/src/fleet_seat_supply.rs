//! Inactive counting contract; no scheduler, requests, admission or merge action.
//! Unwitnessed family counts by that leg's route-table family; route-table gpt or any writer family never counts, whatever claimed.
//! Missing witness logs; void retries on a different family.
//! Below quorum: HOLD plus router alarm; router may rule per D-row, logged per SHA.
//! Missing witness alone does not void a seat. Future FLEET-GO-CHECK must use these words.
//! Monitoring requires later recorded ENABLE-FLEET-FOUNDATION ahead of the HIGH wave.
//! Bootstrap pre-enforcement merges remain residual risk, not enable authority.
use crate::fleet_window_catalog::{decode, Window};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeSet;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Policy {
    pub quorum: usize,
    pub sha: String,
    /// Explicit reserved family, not inferred from model spelling.
    pub reserved_gpt_family: String,
    pub writer_families: BTreeSet<String>,
    pub disallowed_pairs: BTreeSet<(String, String)>,
    pub mandatory_no_tools_header: Option<Vec<u8>>,
    pub judge_only: bool,
    pub judge_forbidden_writer_families: BTreeSet<String>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Seat {
    pub identity: Value,
    pub model: String,
    pub protocol: String,
    pub canonical: String,
    pub route_family: Option<String>,
    pub route_observed_at: i64,
    pub route_expires_at: i64,
    pub route_provenance: String,
    pub catalog_provenance: String,
    pub proving_run: Value,
    pub claimed_family: Option<String>,
    pub witnessed_family: Option<String>,
    /// Historical bytes are never normalized or rewritten.
    pub header: Vec<u8>,
    pub mandatory_no_tools_header: Option<Vec<u8>>,
    pub judge_only: bool,
    pub judge_forbidden_writer_families: BTreeSet<String>,
    pub unavailable: bool,
    pub probe: Probe,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Probe {
    pub successful: bool,
    pub void: bool,
    pub timeout: bool,
    /// Attested by caller; this module does not grant authorization.
    pub authorization_reference: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ruling {
    pub decision_reference: String,
    pub sha: String,
    pub reason: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Input {
    pub epoch_day: i64,
    pub now_s: i64,
    pub monotonic_ms: u64,
    pub previous_monotonic_ms: u64,
    pub catalog_bytes: Vec<u8>,
    pub seats: Vec<Seat>,
    pub ruling: Option<Ruling>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Reading {
    pub seat: Seat,
    pub eligible: bool,
    pub disabled: bool,
    pub findings: BTreeSet<String>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct State {
    pub epoch_day: i64,
    pub monotonic_ms: u64,
    pub sha: String,
    pub families: BTreeSet<String>,
    pub hold: bool,
    pub findings: BTreeSet<String>,
    pub readings: Vec<Reading>,
    /// Authority record only; original HOLD, count and alarm remain unchanged.
    pub ruling: Option<Ruling>,
}
fn text(s: &str) -> bool {
    !s.trim().is_empty()
}
fn sha(s: &str) -> bool {
    matches!(s.len(), 40 | 64) && s.bytes().all(|b| b.is_ascii_hexdigit())
}
/// Pure fold of caller-attested observations. No request sink is invoked in part 1.
pub fn count(policy: &Policy, input: &Input) -> State {
    let catalog = decode(&input.catalog_bytes, input.now_s);
    let mut out = State {
        epoch_day: input.epoch_day,
        monotonic_ms: input.monotonic_ms,
        sha: policy.sha.clone(),
        families: BTreeSet::new(),
        hold: true,
        findings: catalog
            .findings
            .iter()
            .map(|f| format!("catalog:{}:{}", f.source, f.reason))
            .collect(),
        readings: vec![],
        ruling: None,
    };
    let valid = policy.quorum > 0
        && sha(&policy.sha)
        && text(&policy.reserved_gpt_family)
        && policy.writer_families.iter().all(|f| text(f))
        && input.now_s.div_euclid(86400) == input.epoch_day
        && input.monotonic_ms >= input.previous_monotonic_ms;
    if !valid {
        out.findings.insert("invalid_configuration_or_clock".into());
    }
    let unconsumed_pairs = policy.disallowed_pairs.iter().any(|(w, s)| {
        !policy.writer_families.contains(w) || !input.seats.iter().any(|v| &v.canonical == s)
    });
    if unconsumed_pairs {
        out.findings.insert("unconsumed_policy_pair".into());
    }
    let mut seen = BTreeSet::new();
    for seat in &input.seats {
        let mut findings = BTreeSet::new();
        if seat.witnessed_family.is_none() {
            findings.insert("missing_witness".into());
        }
        for evidence in [&seat.claimed_family, &seat.witnessed_family] {
            if evidence.is_some() && evidence != &seat.route_family {
                findings.insert("family_disagreement".into());
            }
        }
        let route = seat.route_family.as_deref().filter(|f| text(f));
        let route_valid = route.is_some()
            && text(&seat.route_provenance)
            && seat.route_observed_at <= input.now_s
            && input.now_s < seat.route_expires_at;
        let proved = catalog.records.iter().any(|r| {
            let entry = catalog.evidence["entries"]
                .as_array()
                .and_then(|entries| entries.iter().find(|e| e["identity"] == seat.identity));
            r.identity == seat.identity
                && matches!(r.window, Window::Known(_))
                && entry.is_some_and(|e| {
                    text(&seat.catalog_provenance)
                        && e[&r.selected_source]["provenance"] == seat.catalog_provenance
                })
                && r.coverage.iter().any(|c| {
                    c.model == seat.model
                        && c.protocol == seat.protocol
                        && c.proving_run == seat.proving_run
                        && matches!(c.window, Window::Known(_))
                })
        });
        let forbidden = route
            .is_some_and(|f| f == policy.reserved_gpt_family || policy.writer_families.contains(f))
            || policy
                .disallowed_pairs
                .iter()
                .any(|(_, s)| s == &seat.canonical)
            || policy.writer_families.iter().any(|w| {
                (seat.judge_only && seat.judge_forbidden_writer_families.contains(w))
                    || (policy.judge_only && policy.judge_forbidden_writer_families.contains(w))
            });
        let header_ok = [
            &policy.mandatory_no_tools_header,
            &seat.mandatory_no_tools_header,
        ]
        .into_iter()
        .all(|required| {
            required
                .as_ref()
                .is_none_or(|h| !h.is_empty() && seat.header == *h)
        });
        if !route_valid || !proved {
            findings.insert("unknown_catalog_or_route_evidence".into());
        }
        if forbidden || !header_ok {
            findings.insert("forbidden_pairing_or_missing_header".into());
        }
        let success = seat.probe.successful && !seat.probe.void && !seat.probe.timeout;
        let disabled = seat.unavailable
            && !(valid
                && !unconsumed_pairs
                && success
                && text(&seat.probe.authorization_reference)
                && route_valid
                && proved
                && !forbidden
                && header_ok);
        let eligible = valid
            && !unconsumed_pairs
            && route_valid
            && proved
            && !forbidden
            && header_ok
            && !disabled
            && success
            && text(&seat.canonical)
            && text(&seat.model)
            && text(&seat.protocol);
        if eligible && seen.insert(seat.canonical.clone()) {
            out.families.insert(route.unwrap().into());
        }
        out.readings.push(Reading {
            seat: seat.clone(),
            eligible,
            disabled,
            findings,
        });
    }
    out.hold = !valid || unconsumed_pairs || out.families.len() < policy.quorum;
    if out.hold {
        out.findings.insert("router_alarm".into());
    }
    if let Some(r) = &input.ruling {
        if sha(&policy.sha) && r.sha == policy.sha && text(&r.decision_reference) && text(&r.reason)
        {
            out.ruling = Some(r.clone());
        } else {
            out.hold = true;
            out.findings.insert("invalid_ruling".into());
            out.findings.insert("router_alarm".into());
        }
    }
    out
}

#[cfg(test)]
#[path = "fleet_seat_supply_tests.rs"]
mod tests;
