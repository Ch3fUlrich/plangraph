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

/// A CI result of the run in flight.
pub fn ci(m: &mut Model, train: TrainId, green: bool) -> Outcome {
    let run = m.run(train);
    ok(m, Event::CiResult { train, run, green })
}

/// A red CI of the run in flight that names its failing part.
pub fn red(m: &mut Model, train: TrainId, culprit: PartId) -> Outcome {
    let run = m.run(train);
    ok(
        m,
        Event::CiRed {
            train,
            run,
            culprit,
        },
    )
}

/// A red round of the run in flight that the builder will fix in place.
pub fn fix_red(m: &mut Model, train: TrainId) -> Outcome {
    let run = m.run(train);
    ok(m, Event::CiRedFix { train, run })
}

/// A refused red round of the run in flight.
pub fn denied_fix_red(m: &mut Model, train: TrainId) -> Denied {
    let run = m.run(train);
    denied(m, Event::CiRedFix { train, run })
}

/// Verify, bind and start the CI of an open train as the builder does.
pub fn start(m: &mut Model, train: TrainId) {
    ok(m, Event::VerifyComplete { train });
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
    ci(m, train, true);
    ok(m, Event::Settle { train });
}
