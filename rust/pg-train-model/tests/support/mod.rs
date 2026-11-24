#![allow(dead_code)]
use pg_train_model::*;

pub fn spec(paths: u16, parent: Option<PartId>) -> PartSpec {
    PartSpec { paths, parent }
}

pub fn policy(target: u8, bound: u8, cut_after: u8, order: Order) -> Policy {
    Policy::new(target, bound, cut_after, order)
}

pub fn ok(m: &mut Model, e: Event) -> Outcome {
    let shown = format!("{e:?}");
    let out = m
        .apply(e)
        .unwrap_or_else(|d| panic!("{shown} denied: {d:?}"));
    assert!(m.invariants_hold(), "invariants broken after {shown}");
    out
}

pub fn denied(m: &mut Model, e: Event) -> Denied {
    let shown = format!("{e:?}");
    let before = m.clone();
    let d = m.apply(e).expect_err(&shown);
    assert_eq!(*m, before, "{shown} was denied but changed the model");
    d
}

pub fn register_all(m: &mut Model, parts: impl IntoIterator<Item = PartId>) {
    for part in parts {
        ok(m, Event::Register { part });
    }
}

/// Bind and start the CI of an open train as the builder does.
pub fn start(m: &mut Model, train: TrainId) {
    let base = m.main();
    ok(
        m,
        Event::Bind {
            train,
            build: train,
            base,
            policy: POLICY,
        },
    );
    ok(m, Event::StartCi { train });
}

pub fn disjoint(n: u8) -> Vec<PartSpec> {
    (0..n).map(|i| spec(1 << i, None)).collect()
}

/// Land an open train as the builder and CI do.
pub fn land(m: &mut Model, train: TrainId) {
    start(m, train);
    ok(m, Event::CiResult { train, green: true });
    ok(m, Event::Settle { train });
}
