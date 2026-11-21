use super::main_state::State;
use super::train::*;
use std::collections::BTreeMap;

fn part(pr: u64, parent: Option<u64>) -> PartIn {
    PartIn {
        pr,
        head: format!("h{pr}"),
        parent: parent.map(|q| Parent {
            pr: q,
            head: format!("h{q}"),
        }),
    }
}

#[test]
fn the_stack_order_puts_every_part_after_its_parent_and_leaves_cycles_last() {
    let order = stack_order(&[
        part(3, Some(2)),
        part(1, None),
        part(2, Some(1)),
        part(4, None),
    ]);
    assert_eq!(
        order.iter().map(|p| p.pr).collect::<Vec<_>>(),
        vec![1, 2, 4, 3]
    );
    let cyc = stack_order(&[part(1, Some(2)), part(2, Some(1)), part(5, None)]);
    assert_eq!(cyc.iter().map(|p| p.pr).collect::<Vec<_>>(), vec![5, 1, 2]);
}

#[test]
fn a_part_is_includable_only_after_its_parent_with_exactly_the_registered_head() {
    let child = part(2, Some(1));
    let none = |_: &str| false;
    assert!(!includable(&child, &BTreeMap::new(), &none));
    assert!(includable(
        &child,
        &BTreeMap::from([(1, "h1".to_owned())]),
        &none
    ));
    assert!(
        !includable(&child, &BTreeMap::from([(1, "other".to_owned())]), &none),
        "the parent merged with another head"
    );
    assert!(
        includable(&child, &BTreeMap::new(), &|s: &str| s == "h1"),
        "the parent already landed"
    );
    assert!(includable(&part(1, None), &BTreeMap::new(), &none));
}

#[test]
fn the_freeze_refuses_another_policy_base_grant_or_selection() {
    let snapshot = vec![part(1, None), part(2, Some(1))];
    let open = BuildOpen {
        build: "b".into(),
        policy_version: "p1".into(),
        base: "base".into(),
        grant_digest: "g".into(),
        snapshot_position: 7,
    };
    let freeze = |f: &dyn Fn(&mut Freeze)| {
        let mut x = Freeze {
            policy_version: "p1".into(),
            base: "base".into(),
            grant_digest: "g".into(),
            selection: snapshot.clone(),
        };
        f(&mut x);
        check_freeze(&open, &snapshot, &x)
    };
    assert_eq!(freeze(&|_| {}), Ok(()));
    assert_eq!(
        freeze(&|f| f.policy_version = "p2".into()),
        Err(FreezeRefusal::PolicyVersion)
    );
    assert_eq!(freeze(&|f| f.base = "b2".into()), Err(FreezeRefusal::Base));
    assert_eq!(
        freeze(&|f| f.grant_digest = "g2".into()),
        Err(FreezeRefusal::GrantDigest)
    );
    // A part that became READY, or a part with another head, between the snapshot and the freeze.
    assert_eq!(
        freeze(&|f| f.selection.push(part(3, None))),
        Err(FreezeRefusal::SelectionChanged)
    );
    assert_eq!(
        freeze(&|f| f.selection[0].head = "moved".into()),
        Err(FreezeRefusal::SelectionChanged)
    );
    assert_eq!(
        freeze(&|f| f.selection[1].parent = None),
        Err(FreezeRefusal::SelectionChanged)
    );
}

#[test]
fn no_train_is_built_while_main_is_not_green() {
    assert_eq!(require_open_main(&State::Green), Ok(()));
    assert_eq!(
        require_open_main(&State::Red { blocking: vec![] }),
        Err("main_red")
    );
    assert_eq!(require_open_main(&State::Pending), Err("main_pending"));
    assert_eq!(
        require_open_main(&State::StateUnknown {
            failing_read: String::new()
        }),
        Err("state_unknown")
    );
}

fn left(head: &str, reason: Reason) -> LeftOut {
    LeftOut {
        pr: 1,
        head: head.into(),
        reason,
        prefix: String::new(),
    }
}

#[test]
fn three_verified_conflict_builds_in_a_row_escalate_once_with_a_stable_id() {
    let mut c = ConflictCounter::default();
    assert!(c.record("b1", &[left("h", Reason::Conflict)]).is_empty());
    assert!(c.record("b2", &[left("h", Reason::Conflict)]).is_empty());
    assert!(
        c.record("b2", &[left("h", Reason::Conflict)]).is_empty(),
        "a retry of the same build counts once"
    );
    assert_eq!(
        c.record("b3", &[left("h", Reason::Conflict)]),
        vec!["h:b3".to_owned()]
    );
    assert!(
        c.record("b4", &[left("h", Reason::Conflict)]).is_empty(),
        "escalated once"
    );
}

#[test]
fn a_descendant_exclusion_or_a_clean_build_resets_the_streak() {
    let mut c = ConflictCounter::default();
    c.record("b1", &[left("h", Reason::Conflict)]);
    c.record("b2", &[left("h", Reason::Descendant)]);
    assert!(c.record("b3", &[left("h", Reason::Conflict)]).is_empty());
    c.record("b4", &[]);
    assert!(c.record("b5", &[left("h", Reason::Conflict)]).is_empty());
}
