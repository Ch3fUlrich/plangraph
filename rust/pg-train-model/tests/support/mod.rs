#![allow(dead_code)]
use pg_train_model::*;

pub fn spec(paths: u16, parent: Option<PartId>) -> PartSpec {
    PartSpec { paths, parent }
}

pub fn policy(target: u8, bound: u8, cut_after: u8, order: Order) -> Policy {
    Policy {
        target,
        bound,
        cut_after,
        order,
    }
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
