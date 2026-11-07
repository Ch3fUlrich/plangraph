//! Shared host-independent safety boundary. No local fallback or network implementation.
use crate::{validate, Capabilities, Envelope, Refusal, Validated};

#[path = "ledger.rs"]
mod ledger;
#[cfg(any(test, feature = "mock"))]
#[path = "mock_transport.rs"]
mod mock_transport;
#[path = "readback.rs"]
mod readback;
pub use ledger::Blocked;
use ledger::{BLOCKED_LIMIT, INVALID_SCOPE};
#[cfg(any(test, feature = "mock"))]
pub use mock_transport::MockTransport;
use readback::matching;

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    Refused(Refusal),
    Outage,
    Conflict,
    Unknown,
    /// Verification failed after a possible upstream commit; this is not a rollback.
    Unverified,
    WritesDisabled,
    InvalidScope,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Receipt(pub String);

#[derive(Clone, Debug)]
pub struct Saved {
    pub envelope: Envelope,
    pub receipt: Receipt,
    pub superseded: bool,
}

pub struct Snapshot {
    pub graph_commit: String,
    pub facts: Vec<Saved>,
}

/// Implementations must atomically upsert nodes AND edges, preserving provenance,
/// scoped by (project, kind, slug). A 412 is Conflict and has no effect.
/// Unknown means the request may have committed; never automatically replay it.
pub trait Transport {
    fn read(&mut self, project: &str) -> Result<Snapshot, Error>;
    fn commit(&mut self, fact: &Validated, if_graph_commit: &str) -> Result<Receipt, Error>;
}

pub struct Recall<'a> {
    pub saved: Vec<Envelope>,
    pub retained_blocked: Vec<&'a Blocked>,
    pub stale: bool,
}

pub struct Memory<T> {
    pub transport: T,
    pub writes_enabled: bool,
    blocked: Vec<Blocked>,
}

impl<T: Transport> Memory<T> {
    pub fn new(transport: T) -> Self {
        Self {
            transport,
            writes_enabled: true,
            blocked: vec![],
        }
    }

    pub fn recall(&mut self, project: &str) -> Result<Recall<'_>, Error> {
        if !crate::slug_ok(project) || project.starts_with("routing-") {
            return Err(Error::InvalidScope);
        }
        let snapshot = self.transport.read(project)?;
        Ok(Recall {
            saved: snapshot
                .facts
                .into_iter()
                .filter(|f| f.envelope.project == project && !f.superseded)
                .map(|f| f.envelope)
                .collect(),
            retained_blocked: self
                .blocked
                .iter()
                .filter(|b| b.project == project)
                .collect(),
            stale: false,
        })
    }

    pub fn remember(
        &mut self,
        project: &str,
        e: &Envelope,
        caps: &Capabilities,
    ) -> Result<Receipt, Error> {
        let result = self.save(project, e, caps);
        if let Err(ref reason) = result {
            // Errors contain only static variants, never caller payloads.
            if self.blocked.len() == BLOCKED_LIMIT {
                self.blocked.remove(0);
            }
            self.blocked.push(Blocked {
                project: if crate::slug_ok(&e.project) && !e.project.starts_with("routing-") {
                    e.project.clone()
                } else {
                    INVALID_SCOPE.to_owned()
                },
                reason: ledger::reason(reason),
            });
        }
        result
    }

    fn save(&mut self, project: &str, e: &Envelope, caps: &Capabilities) -> Result<Receipt, Error> {
        if !crate::slug_ok(project) || project.starts_with("routing-") || e.project != project {
            return Err(Error::InvalidScope);
        }
        if !self.writes_enabled {
            return Err(Error::WritesDisabled);
        }
        let validated = validate(e, caps).map_err(Error::Refused)?;
        for _ in 0..3 {
            let before = self.transport.read(&e.project)?;
            // Enforce idempotence here, not in the transport: an equal fact (including
            // its edges) skips the entire commit, including after unknown outcomes.
            if let Some(r) = matching(&before, e) {
                return Ok(r);
            }
            let receipt = match self.transport.commit(&validated, &before.graph_commit) {
                Ok(r) => Some(r),
                Err(Error::Conflict) => continue, // next attempt MUST re-read
                Err(Error::Unknown) => None,
                Err(e) => return Err(e),
            };
            let after = self.transport.read(&e.project)?;
            let verified = matching(&after, e).ok_or(Error::Unverified)?;
            if receipt.as_ref().is_some_and(|r| r != &verified) {
                return Err(Error::Unverified);
            }
            return Ok(verified);
        }
        Err(Error::Conflict)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn setup() -> (Memory<MockTransport>, Envelope, Capabilities) {
        (
            Memory::new(MockTransport::default()),
            crate::tests::fact(),
            Capabilities::from_schema(include_str!("../tests/fixtures/memory.pg")),
        )
    }
    #[test]
    fn outage_remember_and_recall_fail_closed() {
        let (mut m, e, c) = setup();
        m.transport.outage = true;
        assert_eq!(m.remember(&e.project, &e, &c), Err(Error::Outage));
        assert!(matches!(m.recall("global"), Err(Error::Outage)));
        assert_eq!(m.transport.writes, 0);
        assert_eq!(m.blocked[0].reason, Error::Outage);
    }
    #[test]
    fn explicit_scope_and_superseded_filtering() {
        let (mut m, mut e, c) = setup();
        m.remember(&e.project, &e, &c).unwrap();
        e.project = "plangraph".into();
        m.remember(&e.project, &e, &c).unwrap();
        assert_eq!(m.recall("global").unwrap().saved.len(), 1);
        assert_eq!(m.recall("plangraph").unwrap().saved.len(), 1);
        m.transport.facts[0].superseded = true;
        assert!(m.recall("global").unwrap().saved.is_empty());
        assert!(matches!(m.recall(""), Err(Error::InvalidScope)));
    }
    #[test]
    fn conditional_412_rereads_without_unconditional_fallback() {
        let (mut m, e, c) = setup();
        m.transport.conflict_once = true;
        m.remember(&e.project, &e, &c).unwrap();
        assert_eq!((m.transport.reads, m.transport.writes), (3, 2));
        assert_eq!(m.transport.facts.len(), 1);
    }
    #[test]
    fn unknown_reconciles_and_node_edge_retry_is_duplicate_free() {
        let (mut m, e, c) = setup();
        m.transport.lose_response_once = true;
        let receipt = m.remember(&e.project, &e, &c).unwrap();
        assert_eq!(m.remember(&e.project, &e, &c).unwrap(), receipt);
        assert_eq!((m.transport.reads, m.transport.writes), (3, 1));
        assert_eq!((m.transport.facts.len(), m.transport.edges.len()), (1, 1));
    }
    #[test]
    fn receipt_and_readback_are_both_required() {
        for missing_receipt in [true, false] {
            let (mut m, e, c) = setup();
            m.transport.omit_receipt = missing_receipt;
            m.transport.omit_write = !missing_receipt;
            assert_eq!(m.remember(&e.project, &e, &c), Err(Error::Unverified));
        }
    }
    #[test]
    fn unknown_without_readback_is_not_replayed() {
        let (mut m, e, c) = setup();
        m.transport.lose_response_once = true;
        m.transport.omit_write = true;
        assert_eq!(m.remember(&e.project, &e, &c), Err(Error::Unverified));
        assert_eq!((m.transport.reads, m.transport.writes), (2, 1));
    }
    #[test]
    fn stale_precondition_has_no_node_or_edge_effect() {
        let (_, e, c) = setup();
        let mut m = Memory::new(AlteredTransport {
            stale: true,
            inner: MockTransport::default(),
        });
        assert_eq!(m.remember(&e.project, &e, &c), Err(Error::Conflict));
        assert_eq!(m.blocked[0].reason, Error::Conflict);
        assert_eq!((m.transport.inner.reads, m.transport.inner.writes), (3, 3));
        assert!(m.transport.inner.facts.is_empty());
        assert!(m.transport.inner.edges.is_empty());
    }
    // Fault injection stays local: stale snapshots or a dishonest commit receipt.
    struct AlteredTransport {
        stale: bool,
        inner: MockTransport,
    }
    impl Transport for AlteredTransport {
        fn read(&mut self, project: &str) -> Result<Snapshot, Error> {
            let mut snapshot = self.inner.read(project)?;
            if self.stale {
                snapshot.graph_commit = "stale".into();
            }
            Ok(snapshot)
        }
        fn commit(&mut self, fact: &Validated, expected: &str) -> Result<Receipt, Error> {
            self.inner.commit(fact, expected)?;
            Ok(Receipt("mismatched".into()))
        }
    }
    #[test]
    fn mismatched_commit_receipt_fails_remember() {
        let (_, e, c) = setup();
        let mut m = Memory::new(AlteredTransport {
            stale: false,
            inner: MockTransport::default(),
        });
        assert_eq!(m.remember(&e.project, &e, &c), Err(Error::Unverified));
        // Unverified is possibly committed, not a validation rejection or rollback.
        assert_eq!(m.transport.inner.facts.len(), 1);
        assert_eq!(m.recall(&e.project).unwrap().saved.len(), 1);
        // A subsequent read reconciles the committed fact without another write.
        assert!(m.remember(&e.project, &e, &c).is_ok());
        assert_eq!(m.transport.inner.writes, 1);
    }
    #[test]
    fn superseded_identical_fact_does_not_satisfy_remember() {
        let (mut m, e, c) = setup();
        let old = m.remember(&e.project, &e, &c).unwrap();
        m.transport.facts[0].superseded = true;
        let new = m.remember(&e.project, &e, &c).unwrap();
        assert_ne!(old, new);
        assert_eq!(m.transport.writes, 2);
        assert!(!m.transport.facts[0].superseded);
    }
    #[test]
    fn invalid_projects_refuse_remember_and_recall_without_transport() {
        for project in ["routing-private", "", "Bad Project"] {
            let (mut m, mut e, c) = setup();
            e.project = project.into();
            assert_eq!(m.remember(project, &e, &c), Err(Error::InvalidScope));
            assert!(matches!(m.recall(project), Err(Error::InvalidScope)));
            assert_eq!((m.transport.reads, m.transport.writes), (0, 0));
        }
    }
    #[test]
    fn scope_must_match_envelope_before_transport() {
        let (mut m, e, c) = setup();
        assert_eq!(m.remember("plangraph", &e, &c), Err(Error::InvalidScope));
        assert_eq!((m.transport.reads, m.transport.writes), (0, 0));
        assert!(m.recall("plangraph").unwrap().retained_blocked.is_empty());
        let recall = m.recall(&e.project).unwrap();
        assert_eq!(recall.retained_blocked.len(), 1);
        assert_eq!(recall.retained_blocked[0].reason, Error::InvalidScope);
    }
    #[test]
    fn invalid_envelope_scopes_are_redacted_including_debug() {
        for project in ["[REDACTED]", "Bad Project", "routing-private", ""] {
            let (mut m, mut e, c) = setup();
            e.project = project.into();
            assert_eq!(m.remember(project, &e, &c), Err(Error::InvalidScope));
            assert_eq!(m.blocked[0].project, INVALID_SCOPE);
            assert_eq!(m.blocked[0].reason, Error::InvalidScope);
            assert_eq!(
                format!("{:?}", m.blocked[0]),
                "Blocked { project: \"<invalid-scope>\", reason: InvalidScope }"
            );
        }
    }
    #[test]
    fn blocked_ledger_is_bounded_and_evicts_oldest() {
        let (mut m, mut e, c) = setup();
        m.writes_enabled = false;
        assert_eq!(m.remember("global", &e, &c), Err(Error::WritesDisabled));
        e.project = "plangraph".into();
        for _ in 0..BLOCKED_LIMIT + 1 {
            assert_eq!(m.remember(&e.project, &e, &c), Err(Error::WritesDisabled));
            assert!(m.blocked.len() <= BLOCKED_LIMIT);
        }
        assert_eq!(m.blocked.len(), BLOCKED_LIMIT);
        assert!(m.recall("global").unwrap().retained_blocked.is_empty());
    }

    // Deliberately appends every node and edge: no retain, set, or upsert dedupe.
    #[derive(Default)]
    struct AppendTransport {
        facts: Vec<Saved>,
        edges: Vec<(String, String, String)>,
        lose_response: bool,
    }
    impl Transport for AppendTransport {
        fn read(&mut self, _: &str) -> Result<Snapshot, Error> {
            Ok(Snapshot {
                graph_commit: self.facts.len().to_string(),
                facts: self.facts.clone(),
            })
        }
        fn commit(&mut self, v: &Validated, expected: &str) -> Result<Receipt, Error> {
            if expected != self.facts.len().to_string() {
                return Err(Error::Conflict);
            }
            let e = v.envelope();
            let receipt = Receipt((self.facts.len() + 1).to_string());
            self.facts.push(Saved {
                envelope: e.clone(),
                receipt: receipt.clone(),
                superseded: false,
            });
            if !v.hub_edge().is_empty() {
                self.edges
                    .push((e.project.clone(), e.slug.clone(), v.hub_edge().into()));
            }
            for r in &e.relations {
                self.edges
                    .push((r.target_slug.clone(), e.slug.clone(), r.edge.clone()));
            }
            if std::mem::take(&mut self.lose_response) {
                return Err(Error::Unknown);
            }
            Ok(receipt)
        }
    }
    #[test]
    fn save_skips_equal_fact_and_edges_on_non_deduplicating_transport() {
        for lose_response in [false, true] {
            let (_, e, c) = setup();
            let mut m = Memory::new(AppendTransport {
                lose_response,
                ..Default::default()
            });
            let receipt = m.remember(&e.project, &e, &c).unwrap();
            for _ in 0..3 {
                assert_eq!(m.remember(&e.project, &e, &c).unwrap(), receipt);
            }
            assert_eq!(m.transport.facts.len(), 1);
            assert_eq!(m.transport.edges.len(), 1);
        }
    }
    #[test]
    fn blocked_ledger_is_project_scoped() {
        let (mut m, mut e, c) = setup();
        m.writes_enabled = false;
        assert_eq!(m.remember("global", &e, &c), Err(Error::WritesDisabled));
        assert!(m.recall("plangraph").unwrap().retained_blocked.is_empty());
        e.project = "plangraph".into();
        assert_eq!(m.remember("plangraph", &e, &c), Err(Error::WritesDisabled));
        for project in ["global", "plangraph"] {
            let recall = m.recall(project).unwrap();
            assert_eq!(recall.retained_blocked.len(), 1);
            assert_eq!(recall.retained_blocked[0].project, project);
        }
    }
    #[test]
    fn rollback_preserves_facts_and_separate_blocked_ledger() {
        let (mut m, e, c) = setup();
        m.remember(&e.project, &e, &c).unwrap();
        m.writes_enabled = false;
        assert_eq!(m.remember(&e.project, &e, &c), Err(Error::WritesDisabled));
        let recall = m.recall("global").unwrap();
        assert_eq!((recall.saved.len(), recall.retained_blocked.len()), (1, 1));
        assert!(!recall.stale);
    }
}
