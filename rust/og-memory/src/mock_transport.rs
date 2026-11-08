//! Deterministic transport for offline callers and tests.
use super::{Error, Receipt, Saved, Snapshot, Transport, Validated};

/// Deterministic in-memory transport; never accesses credentials or the network.
#[derive(Default)]
pub struct MockTransport {
    pub facts: Vec<Saved>,
    pub outage: bool,
    pub conflict_once: bool,
    pub lose_response_once: bool,
    pub omit_receipt: bool,
    pub omit_write: bool,
    pub reads: usize,
    pub writes: usize,
    pub edges: std::collections::BTreeSet<(String, String, String)>,
    revision: usize,
}

impl Transport for MockTransport {
    fn read(&mut self, _: &str) -> Result<Snapshot, Error> {
        self.reads += 1;
        if self.outage {
            return Err(Error::Outage);
        }
        Ok(Snapshot {
            graph_commit: self.revision.to_string(),
            facts: self.facts.clone(),
        })
    }
    fn commit(&mut self, v: &Validated, expected: &str) -> Result<Receipt, Error> {
        if self.outage {
            return Err(Error::Outage);
        }
        self.writes += 1;
        if std::mem::take(&mut self.conflict_once) {
            self.revision += 1;
            return Err(Error::Conflict);
        }
        if expected != self.revision.to_string() {
            return Err(Error::Conflict);
        }
        self.revision += 1;
        let receipt = Receipt(if self.omit_receipt {
            String::new()
        } else {
            self.revision.to_string()
        });
        let e = v.envelope();
        if !self.omit_write {
            self.facts.retain(|f| {
                !(f.envelope.project == e.project
                    && f.envelope.kind == e.kind
                    && f.envelope.slug == e.slug)
            });
            self.facts.push(Saved {
                envelope: e.clone(),
                receipt: receipt.clone(),
                superseded: false,
            });
            if !v.hub_edge().is_empty() {
                self.edges
                    .insert((e.project.clone(), e.slug.clone(), v.hub_edge().into()));
            }
            for r in &e.relations {
                self.edges
                    .insert((r.target_slug.clone(), e.slug.clone(), r.edge.clone()));
            }
        }
        if std::mem::take(&mut self.lose_response_once) {
            return Err(Error::Unknown);
        }
        Ok(receipt)
    }
}
