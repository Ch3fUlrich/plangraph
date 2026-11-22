use super::split::*;
use super::train::{Parent, PartIn, PartReceipt};

fn part(pr: u64, parent: Option<u64>) -> PartIn {
    PartIn {
        pr,
        head: format!("h{pr}"),
        parent: parent.map(|q| Parent {
            pr: q,
            head: format!("h{q}"),
        }),
        receipt: PartReceipt {
            run_id: format!("run-{pr}"),
            head: format!("h{pr}"),
        },
    }
}

fn prs(parts: &[PartIn]) -> Vec<u64> {
    parts.iter().map(|p| p.pr).collect()
}

fn freeze(actions: &[Action]) -> Vec<&Candidate> {
    actions
        .iter()
        .filter_map(|a| match a {
            Action::Freeze(c) => Some(c),
            _ => None,
        })
        .collect()
}

/// Every candidate must be closed: each part's parent is in it (or not a part of the train at all).
fn closed(c: &Candidate, universe: &[PartIn]) -> bool {
    c.parts.iter().all(|p| {
        p.parent.as_ref().is_none_or(|q| {
            !universe.iter().any(|u| u.pr == q.pr) || c.parts.iter().any(|x| x.pr == q.pr)
        })
    })
}

#[test]
fn a_train_with_one_bad_part_lands_the_others_in_the_next_candidate_and_returns_the_bad_one_with_its_log(
) {
    let train = vec![part(1, None), part(2, None), part(3, None), part(4, None)];
    let (mut s, first) = Session::new(train);
    let out = s.red(&first.id, &[3], "job log: test x failed");
    assert_eq!(
        out[0],
        Action::Evict {
            pr: 3,
            head: "h3".into(),
            log: "job log: test x failed".into(),
            to: Owner::Writer
        }
    );
    let next = freeze(&out);
    assert_eq!(prs(&next[0].parts), vec![1, 2, 4]);
    let landed = s.green(&next[0].id);
    assert!(matches!(&landed[0], Action::Land(c) if prs(&c.parts) == vec![1, 2, 4]));
}

#[test]
fn a_part_evicted_twice_goes_to_routing() {
    let (mut s, first) = Session::new(vec![part(1, None), part(2, None)]);
    let out = s.red(&first.id, &[2], "log");
    assert!(matches!(
        &out[0],
        Action::Evict {
            pr: 2,
            to: Owner::Writer,
            ..
        }
    ));
    // The next train brings the part back and it fails again: its second eviction goes to routing.
    let (mut again, c) =
        Session::with_strikes(vec![part(2, None), part(3, None)], s.strikes().clone());
    let out = again.red(&c.id, &[2], "log");
    assert!(matches!(
        &out[0],
        Action::Evict {
            pr: 2,
            to: Owner::Routing,
            ..
        }
    ));
}

#[test]
fn stacked_parts_are_never_split_from_their_parents() {
    // Stacks: 1 <- 2 <- 3, 4 <- 5, and 6 alone.
    let train = vec![
        part(1, None),
        part(2, Some(1)),
        part(3, Some(2)),
        part(4, None),
        part(5, Some(4)),
        part(6, None),
    ];
    for hint in [vec![], vec![2], vec![3], vec![5], vec![4], vec![6], vec![1]] {
        let (mut s, first) = Session::new(train.clone());
        let mut work = vec![first];
        let mut guard = 0;
        while let Some(c) = work.pop() {
            guard += 1;
            assert!(guard < 50);
            assert!(
                closed(&c, &train),
                "candidate {:?} splits a stack (hint {hint:?})",
                prs(&c.parts)
            );
            let out = s.red(&c.id, &hint, "log");
            work.extend(freeze(&out).into_iter().cloned());
        }
    }
}

#[test]
fn halves_are_cut_at_stack_boundaries_and_may_be_uneven() {
    // Chains of lengths 1 and 3: the halves are 1 and 3.
    let train = vec![
        part(1, None),
        part(2, None),
        part(3, Some(2)),
        part(4, Some(3)),
    ];
    let (mut s, first) = Session::new(train);
    let out = s.red(&first.id, &[], "log");
    let halves = freeze(&out);
    assert_eq!(halves.len(), 2);
    assert_eq!(
        (prs(&halves[0].parts), prs(&halves[1].parts)),
        (vec![1], vec![2, 3, 4])
    );
}

#[test]
fn one_failing_stack_is_cut_at_a_prefix_and_its_descendants_are_blocked_until_the_prefix_lands() {
    let train = vec![
        part(1, None),
        part(2, Some(1)),
        part(3, Some(2)),
        part(4, Some(3)),
    ];
    let (mut s, first) = Session::new(train);
    let out = s.red(&first.id, &[], "log");
    assert_eq!(out[0], Action::Blocked(vec![3, 4]));
    let prefix = freeze(&out)[0].clone();
    assert_eq!(prs(&prefix.parts), vec![1, 2]);
    assert_eq!(prefix.kind, Kind::Prefix);
    let landed = s.green(&prefix.id);
    assert!(matches!(&landed[0], Action::Land(c) if prs(&c.parts) == vec![1, 2]));
    // Nothing above the prefix moves before its exact head is verified in main.
    assert!(s.landed(&prefix.id, false).is_empty());
    let rebuilt = s.landed(&prefix.id, true);
    assert!(
        matches!(&rebuilt[0], Action::Rebuild(c) if prs(&c.parts) == vec![3, 4] && c.kind == Kind::Remainder)
    );
}

#[test]
fn the_hinted_part_takes_its_descendants_out_without_blaming_them() {
    let train = vec![
        part(1, None),
        part(2, Some(1)),
        part(3, Some(2)),
        part(4, None),
    ];
    let (mut s, first) = Session::new(train);
    let out = s.red(&first.id, &[2], "log");
    assert!(matches!(&out[0], Action::Evict { pr: 2, .. }));
    assert_eq!(out[1], Action::Blocked(vec![3]));
    assert_eq!(prs(&freeze(&out)[0].parts), vec![1, 4]);
    assert_eq!(prs(s.deferred()), vec![3]);
}

#[test]
fn two_halves_that_pass_alone_land_one_after_the_other_with_a_second_full_ci() {
    let train = vec![part(1, None), part(2, None), part(3, None)];
    let (mut s, first) = Session::new(train);
    let halves: Vec<Candidate> = freeze(&s.red(&first.id, &[], "log"))
        .into_iter()
        .cloned()
        .collect();
    assert_eq!(halves.len(), 2);
    assert!(s.green(&halves[0].id).is_empty(), "wait for the sibling");
    let landing = s.green(&halves[1].id);
    assert!(matches!(&landing[0], Action::Land(c) if c.id == halves[0].id));
    // After the first landed and is verified in main, the second is rebuilt and runs full CI again.
    let rebuild = s.landed(&halves[0].id, true);
    let Action::Rebuild(second) = &rebuild[0] else {
        panic!("{rebuild:?}")
    };
    assert_eq!(
        (prs(&second.parts), &second.kind),
        (prs(&halves[1].parts), &Kind::Second)
    );
    assert!(matches!(&s.green(&second.id)[0], Action::Land(c) if c.id == second.id));
}

#[test]
fn a_red_second_run_leaves_the_second_half_unmerged() {
    let (mut s, first) = Session::new(vec![part(1, None), part(2, None)]);
    let halves: Vec<Candidate> = freeze(&s.red(&first.id, &[], "log"))
        .into_iter()
        .cloned()
        .collect();
    s.green(&halves[0].id);
    s.green(&halves[1].id);
    let Action::Rebuild(second) = s.landed(&halves[0].id, true).remove(0) else {
        panic!()
    };
    let out = s.red(&second.id, &[2], "log");
    assert_eq!(out, vec![Action::Unmerged(second.parts)]);
}

#[test]
fn the_train_size_starts_at_four_grows_by_one_on_green_halves_on_red_within_one_and_eight() {
    let mut size = TrainSize::default();
    assert_eq!(size.0, 4);
    size = size.after(true);
    assert_eq!(size.0, 5);
    for _ in 0..10 {
        size = size.after(true);
    }
    assert_eq!(size.0, 8);
    assert_eq!(size.after(false).0, 4);
    let mut small = TrainSize(2);
    small = small.after(false);
    assert_eq!(small.0, 1);
    assert_eq!(small.after(false).0, 1);
}

fn two_halves() -> (Session, Vec<Candidate>) {
    let (mut s, first) = Session::new(vec![
        part(1, None),
        part(2, None),
        part(3, None),
        part(4, None),
    ]);
    let halves: Vec<Candidate> = freeze(&s.red(&first.id, &[], "log"))
        .into_iter()
        .cloned()
        .collect();
    assert_eq!(halves.len(), 2);
    (s, halves)
}

#[test]
fn a_half_that_passed_before_its_sibling_went_red_lands_when_the_sibling_is_handled() {
    let (mut s, halves) = two_halves();
    assert!(s.green(&halves[0].id).is_empty(), "waits for the sibling");
    let out = s.red(&halves[1].id, &[3], "log");
    assert!(
        matches!(&out[0], Action::Land(c) if c.id == halves[0].id),
        "the green half is released: {out:?}"
    );
    assert!(
        out.iter().any(|a| matches!(a, Action::Evict { pr: 3, .. })),
        "the red half goes on alone"
    );
}

#[test]
fn a_half_that_goes_red_first_lets_its_green_sibling_land_alone() {
    let (mut s, halves) = two_halves();
    let out = s.red(&halves[0].id, &[1], "log");
    assert!(!out.iter().any(|a| matches!(a, Action::Land(_))));
    let landing = s.green(&halves[1].id);
    assert!(matches!(&landing[0], Action::Land(c) if c.id == halves[1].id));
}

#[test]
fn a_duplicate_green_never_lands_twice_or_skips_the_second_halfs_rebuild() {
    let (mut s, halves) = two_halves();
    s.green(&halves[0].id);
    let both = s.green(&halves[1].id);
    assert!(matches!(&both[0], Action::Land(c) if c.id == halves[0].id));
    // Duplicate notifications for either half release nothing.
    assert!(s.green(&halves[1].id).is_empty());
    assert!(s.green(&halves[0].id).is_empty());
    // The second half is still rebuilt after the first lands, and only once.
    let rebuild = s.landed(&halves[0].id, true);
    assert!(matches!(&rebuild[0], Action::Rebuild(c) if c.kind == Kind::Second));
    assert!(s.landed(&halves[0].id, true).is_empty());
    // A plain candidate lands once as well.
    let (mut one, first) = Session::new(vec![part(9, None)]);
    assert_eq!(one.green(&first.id).len(), 1);
    assert!(one.green(&first.id).is_empty());
}

#[test]
fn a_nested_split_tracks_each_pair_by_candidate_id() {
    // Eight independent parts: halves A=[1..4], B=[5..8]; A goes red and is split again into A1, A2.
    let train: Vec<PartIn> = (1..=8).map(|n| part(n, None)).collect();
    let (mut s, first) = Session::new(train);
    let outer: Vec<Candidate> = freeze(&s.red(&first.id, &[], "log"))
        .into_iter()
        .cloned()
        .collect();
    let (a, b) = (outer[0].clone(), outer[1].clone());
    let inner: Vec<Candidate> = freeze(&s.red(&a.id, &[], "log"))
        .into_iter()
        .cloned()
        .collect();
    assert_eq!(inner.len(), 2);
    // The outer sibling B passes: it must not stand for either inner half.
    let b_green = s.green(&b.id);
    assert!(
        matches!(&b_green[..], [Action::Land(c)] if c.id == b.id),
        "B lands alone (its pair was released): {b_green:?}"
    );
    // Inner half 2 passes first: it waits for inner half 1, B's result is not its sibling's.
    assert!(s.green(&inner[1].id).is_empty());
    let both = s.green(&inner[0].id);
    assert!(
        matches!(&both[..], [Action::Land(c)] if c.id == inner[0].id),
        "{both:?}"
    );
    // The second inner half is rebuilt only after the first landed, and lands only after its own CI.
    let rebuild = s.landed(&inner[0].id, true);
    let Action::Rebuild(second) = &rebuild[0] else {
        panic!("{rebuild:?}")
    };
    assert_eq!(second.kind, Kind::Second);
    assert_eq!(prs(&second.parts), prs(&inner[1].parts));
    assert!(matches!(&s.green(&second.id)[..], [Action::Land(c)] if c.id == second.id));
}
