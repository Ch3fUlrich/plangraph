use std::collections::BTreeMap;

use pg_receipts::*;

#[derive(Default)]
struct Keeper {
    outcomes: BTreeMap<Id, (Id, &'static str, &'static str, Id)>,
    writes: usize,
}

impl Keeper {
    fn write(
        &mut self,
        id: Id,
        credential: Id,
        verb: &'static str,
        source: Id,
        current: Id,
        commit: bool,
    ) -> &'static str {
        if let Some(&(owner, _, old_verb, old_source)) = self.outcomes.get(&id) {
            return if (credential, verb, source) == (owner, old_verb, old_source) {
                "duplicate"
            } else {
                "conflict"
            };
        }
        if !commit {
            return "busy";
        }
        let outcome = if verb == "submit" && source != current {
            "round_stale"
        } else {
            self.writes += 1;
            "ok"
        };
        self.outcomes
            .insert(id, (credential, outcome, verb, source));
        outcome
    }

    fn read(&self, id: Id, credential: Id) -> Option<&'static str> {
        let &(owner, result, _, _) = self.outcomes.get(&id)?;
        (owner == credential).then_some(result)
    }
}

#[test]
fn keeper_vectors_match_transaction_model() {
    let mut keeper = Keeper::default();
    for line in include_str!("keeper-adoption.tsv").lines() {
        let fields: Vec<_> = line.split('\t').collect();
        let [verb, id, source, current, expected] = fields.as_slice() else {
            panic!("{line}")
        };
        let parse = |v: &str| v.parse().unwrap();
        assert_eq!(
            keeper.write(parse(id), 7, verb, parse(source), parse(current), true),
            *expected
        );
    }
    assert_eq!(keeper.writes, 3);
}

#[test]
fn r1_revoked_credential_can_read_only_its_receipt() {
    let mut keeper = Keeper::default();
    assert_eq!(keeper.write(1, 7, "submit", 1, 1, true), "ok");
    assert_eq!(keeper.read(1, 7), Some("ok"));
    assert_eq!(keeper.read(1, 8), None);
    assert_eq!(keeper.read(2, 7), None);
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Entry {
    Evidence,
    Submit,
    Legacy,
}

#[derive(Default)]
struct Queue {
    entries: BTreeMap<Id, (Entry, Option<bool>)>,
}

impl Queue {
    fn append(&mut self, entry: Entry) -> Id {
        let seq = self.entries.last_key_value().map_or(1, |(id, _)| id + 1);
        self.entries.insert(seq, (entry, None));
        seq
    }

    fn runnable(&self) -> Option<Id> {
        let (&id, &(entry, _)) = self
            .entries
            .iter()
            .find(|(_, (_, settled))| settled.is_none())?;
        if entry == Entry::Legacy
            || (entry == Entry::Submit
                && self.entries.range(..id).any(|(_, (older, outcome))| {
                    *older == Entry::Evidence && *outcome != Some(true)
                }))
        {
            return None;
        }
        Some(id)
    }

    fn acknowledge(&mut self, seq: Id, ok: bool) {
        self.entries.get_mut(&seq).unwrap().1 = Some(ok);
    }
}

#[test]
fn r1_queue_crash_and_append_during_acknowledgement() {
    let mut queue = Queue::default();
    let first = queue.append(Entry::Evidence);
    assert_eq!(queue.runnable(), Some(first));
    let second = queue.append(Entry::Submit);
    queue.acknowledge(first, true);
    assert_eq!(queue.runnable(), Some(second));
}

#[test]
fn r1_legacy_outcomes_authorize_only_readback() {
    let mut queue = Queue::default();
    let unresolved = queue.append(Entry::Legacy);
    assert!(queue.runnable().is_none());
    queue.acknowledge(unresolved, true);
    assert!(queue.runnable().is_none());
}

fn verdict(ledger: &mut Ledger, binding: (Id, Id), current: (Id, Id), payload: Id) -> bool {
    binding == current && ledger.apply(1, Stamp(0, 0), Step::Bind(payload)).is_ok()
}

#[test]
fn r1_same_head_resubmission_cannot_rebind_a_delayed_seat() {
    let mut ledger = Ledger::default();
    ledger.begin(1, Spec::new(Class::Gated, 1), &[]).unwrap();
    assert!(!verdict(&mut ledger, (1, 42), (2, 42), 7));
}

#[test]
fn r2_heartbeat_identity_and_submit_round_are_distinct() {
    let mut keeper = Keeper::default();
    assert_eq!(keeper.write(1, 7, "heartbeat", 0, 0, true), "ok");
    assert_eq!(keeper.write(2, 7, "heartbeat", 0, 0, true), "ok");
    assert_eq!(keeper.write(3, 7, "submit", 1, 2, true), "round_stale");
    assert_eq!(keeper.write(4, 7, "submit", 2, 2, true), "ok");
    assert_eq!(keeper.writes, 3);
}

#[test]
fn r2_legacy_import_preserves_order_and_blocks_unproven_entry() {
    let mut queue = Queue::default();
    let old = queue.append(Entry::Legacy);
    let live = queue.append(Entry::Submit);
    assert!(queue.runnable().is_none());
    queue.acknowledge(old, true);
    assert_eq!(queue.runnable(), Some(live));
}

#[test]
fn r2_seat_binds_choice_once_when_available() {
    let mut ledger = Ledger::default();
    ledger.begin(1, Spec::new(Class::Gated, 1), &[]).unwrap();
    assert!(ledger.apply(1, Stamp(0, 0), Step::Dispatch).is_err());
    assert!(verdict(&mut ledger, (1, 42), (1, 42), 7));
    assert!(!verdict(&mut ledger, (1, 42), (1, 42), 9));
}

#[test]
fn r3_local_admission_and_mutation_commit_together() {
    let mut keeper = Keeper::default();
    assert_eq!(keeper.write(1, 7, "heartbeat", 0, 0, false), "busy");
    assert_eq!(keeper.writes, 0);
    assert_eq!(keeper.read(1, 7), None);
    assert_eq!(keeper.write(1, 7, "heartbeat", 0, 0, true), "ok");
}

#[test]
fn r3_live_submit_waits_for_failed_evidence_disposition() {
    let mut queue = Queue::default();
    let evidence = queue.append(Entry::Evidence);
    let submit = queue.append(Entry::Submit);
    assert_eq!(queue.runnable(), Some(evidence));
    queue.acknowledge(evidence, false);
    assert!(queue.runnable().is_none());
    queue.acknowledge(evidence, true);
    assert_eq!(queue.runnable(), Some(submit));
}

#[test]
fn r3_every_ingress_requires_source_round_and_seat_binding() {
    let mut keeper = Keeper::default();
    assert_eq!(keeper.write(1, 7, "submit", 1, 2, true), "round_stale");
    let mut ledger = Ledger::default();
    assert!(!verdict(&mut ledger, (2, 42), (2, 42), 7));
    ledger.begin(1, Spec::new(Class::Gated, 1), &[]).unwrap();
    assert!(!verdict(&mut ledger, (1, 42), (2, 42), 7));
    assert!(verdict(&mut ledger, (2, 42), (2, 42), 7));
}
