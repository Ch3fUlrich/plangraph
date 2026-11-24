mod support;
use pg_train_model::*;
use support::*;

#[test]
fn no_part_joins_a_train_whose_ci_has_started() {
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), disjoint(4));
    register_all(&mut m, [0, 1]);
    ok(&mut m, Event::Tick);
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(0));
    register_all(&mut m, [2]);
    ok(&mut m, Event::Join { part: 2, train: 0 });
    assert_eq!(m.train_parts(0), [0, 1, 2]);
    start(&mut m, 0);
    register_all(&mut m, [3]);
    assert_eq!(
        denied(&mut m, Event::Join { part: 3, train: 0 }),
        Denied::CiStarted
    );
    // The late part waits for the next train; the sealed one lands unchanged.
    ci(&mut m, 0, true);
    ok(&mut m, Event::Settle { train: 0 });
    assert_eq!(m.status(3), Status::Ready);
    assert_eq!(m.status(2), Status::Landed);
}

#[test]
fn a_join_after_a_bind_unbinds_the_train() {
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), disjoint(3));
    register_all(&mut m, [0, 1]);
    ok(&mut m, Event::Tick);
    ok(&mut m, Event::Select);
    let base = m.main();
    ok(
        &mut m,
        Event::Bind {
            train: 0,
            build: 0,
            base,
            policy: POLICY,
        },
    );
    register_all(&mut m, [2]);
    ok(&mut m, Event::Join { part: 2, train: 0 });
    assert_eq!(denied(&mut m, Event::StartCi { train: 0 }), Denied::Unbound);
}

#[test]
fn the_binding_refuses_a_foreign_build_base_or_policy() {
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), disjoint(2));
    register_all(&mut m, [0, 1]);
    ok(&mut m, Event::Tick);
    ok(&mut m, Event::Select);
    let base = m.main();
    let bind = |build, base, policy| Event::Bind {
        train: 0,
        build,
        base,
        policy,
    };
    assert_eq!(denied(&mut m, bind(1, base, POLICY)), Denied::BindMismatch);
    assert_eq!(
        denied(&mut m, bind(0, base + 1, POLICY)),
        Denied::BindMismatch
    );
    assert_eq!(
        denied(&mut m, bind(0, base, POLICY + 1)),
        Denied::BindMismatch
    );
    assert_eq!(denied(&mut m, Event::StartCi { train: 0 }), Denied::Unbound);
    ok(&mut m, bind(0, base, POLICY));
}

#[test]
fn a_train_built_on_a_moved_main_cannot_bind_or_settle_until_rebuilt() {
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), disjoint(2));
    register_all(&mut m, [0, 1]);
    ok(&mut m, Event::Tick);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    ci(&mut m, 0, true);
    ok(&mut m, Event::MainMove);
    assert_eq!(denied(&mut m, Event::Settle { train: 0 }), Denied::Stale);
    ok(&mut m, Event::Rebuild { train: 0 });
    assert_eq!(m.train_ci(0), Ci::NotStarted);
    assert_eq!(denied(&mut m, Event::Settle { train: 0 }), Denied::NotGreen);
    start(&mut m, 0);
    ci(&mut m, 0, true);
    ok(&mut m, Event::Settle { train: 0 });
    assert_eq!(m.main(), 2);
}
