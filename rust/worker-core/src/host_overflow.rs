//! Ordered same-call overflow for writers and seats, using shared admission.
use crate::package_placement::{placement_reason, ranked, Candidate, Policy, Refusal};
use serde::Serialize;
use serde_json::Value;

pub enum Launch {
    Started(Value),
    /// Only an explicit refusal guarantees that no writer was launched.
    Refused(String),
    Uncertain(String),
}
#[derive(Serialize)]
pub struct Outcome {
    pub host: Option<String>,
    pub reason: Option<String>,
    pub receipt: Option<Value>,
    pub refusals: Vec<Refusal>,
    pub queued: bool,
    pub error: Option<String>,
}
pub fn spawn(
    hosts: &[Candidate],
    policy: &Policy,
    now: u64,
    mut launch: impl FnMut(&Candidate) -> Launch,
) -> Outcome {
    let (admitted, refusals) = ranked(hosts, policy, now);
    let mut out = Outcome {
        host: None,
        receipt: None,
        reason: None,
        refusals,
        queued: false,
        error: None,
    };
    for host in admitted {
        match launch(host) {
            Launch::Started(receipt) => {
                out.host = Some(host.id.clone());
                out.reason = Some(placement_reason(host, policy));
                out.receipt = Some(receipt);
                return out;
            }
            Launch::Refused(reason) => out.refusals.push(Refusal {
                host: host.id.clone(),
                reason,
            }),
            Launch::Uncertain(reason) => {
                out.error = Some(format!("{}: {reason}", host.id));
                return out;
            }
        }
    }
    out.queued = true;
    out
}
