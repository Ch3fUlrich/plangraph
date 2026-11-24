use pg_train_model::replay::*;
use pg_train_model::{Order, Rule};

const COSTS: Costs = Costs {
    base_secs: 900,
    per_part_secs: 900,
    per_conflict_secs: 3600,
};

fn history() -> History {
    serde_json::from_str(include_str!("../fixtures/trains_9_16.json")).unwrap()
}

fn batch(id: &str, ready: i64, paths: &[u32]) -> Batch {
    Batch {
        id: id.into(),
        train: 0,
        ready,
        commits: 1,
        paths: paths.to_vec(),
    }
}

fn tiny(batches: Vec<Batch>) -> History {
    History {
        version: 1,
        source: String::new(),
        landed: Default::default(),
        path_count: 0,
        batches,
    }
}

fn rule(target: usize, bound: u32, order: Order) -> Config {
    Config {
        rule: Rule::new(target, bound, order),
        cut_after_secs: 3600,
        costs: COSTS,
        speculative: false,
    }
}

fn with_conflict_cost(mut c: Config, secs: i64) -> Config {
    c.costs.per_conflict_secs = secs;
    c
}

#[test]
fn the_fixture_holds_the_batches_of_trains_nine_to_sixteen() {
    let h = history();
    assert_eq!(h.version, 1);
    assert_eq!(h.batches.len(), 52);
    let trains: std::collections::BTreeSet<u8> = h.batches.iter().map(|b| b.train).collect();
    assert_eq!(
        trains.into_iter().collect::<Vec<_>>(),
        (9..=16).collect::<Vec<u8>>()
    );
    assert_eq!(h.landed.len(), 8);
    assert!(h
        .batches
        .iter()
        .flat_map(|b| &b.paths)
        .all(|&p| (p as usize) < h.path_count));
    assert!(h
        .batches
        .iter()
        .all(|b| b.paths.windows(2).all(|w| w[0] < w[1])));
}

#[test]
fn every_ready_part_rides_in_one_train_and_the_overlap_is_counted() {
    let h = tiny(vec![
        batch("a", 0, &[1, 2]),
        batch("b", 10, &[2]),
        batch("c", 20, &[9]),
    ]);
    let r = replay(&h, &Config::every_ready_rides(COSTS));
    // `a` leaves at once alone; `b` and `c` ride together once it landed.
    assert_eq!(r.trains.len(), 2);
    assert_eq!(r.trains[0].parts, ["a"]);
    assert_eq!(r.trains[1].parts, ["b", "c"]);
    assert_eq!(r.conflicts, 0);
    let all = tiny(vec![
        batch("a", 0, &[2]),
        batch("b", 0, &[2]),
        batch("c", 0, &[2]),
    ]);
    let r = replay(&all, &Config::every_ready_rides(COSTS));
    assert_eq!((r.trains.len(), r.conflicts), (1, 2));
    assert_eq!(r.red_rate, None);
}

#[test]
fn a_bounded_deferral_trades_conflicts_for_more_trains() {
    let all = tiny(vec![
        batch("a", 0, &[2]),
        batch("b", 0, &[2]),
        batch("c", 0, &[2]),
    ]);
    let every = replay(&all, &Config::every_ready_rides(COSTS));
    let deferred = replay(&all, &rule(8, 7200, Order::Oldest));
    assert_eq!(deferred.conflicts, 0);
    assert_eq!(deferred.trains.len(), 3);
    assert!(deferred.trains.len() > every.trains.len() && every.conflicts == 2);
}

#[test]
fn the_bound_forces_a_deferred_part_in_even_when_it_overlaps() {
    let all = tiny(vec![batch("a", 0, &[2]), batch("b", 0, &[2])]);
    let r = replay(&all, &rule(8, 0, Order::Oldest));
    assert_eq!((r.trains.len(), r.conflicts), (1, 1));
}

#[test]
fn every_recorded_batch_lands_exactly_once_under_each_rule() {
    let h = history();
    let rules = [
        Config::every_ready_rides(COSTS),
        rule(2, 3600, Order::Count),
        rule(2, 3600, Order::Oldest),
        rule(4, 7200, Order::Count),
        rule(4, 7200, Order::Oldest),
    ];
    for c in rules {
        let r = replay(&h, &c);
        let mut ids: Vec<&String> = r.trains.iter().flat_map(|t| &t.parts).collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), 52, "{c:?}");
        assert_eq!(r.trains.iter().map(|t| t.parts.len()).sum::<usize>(), 52);
        assert!(r.parts_per_hour > 0.0 && r.longest_wait_secs >= r.mean_wait_secs);
        assert!(r.trains.windows(2).all(|w| w[0].land <= w[1].start));
    }
}

fn speculative(mut c: Config) -> Config {
    c.speculative = true;
    c
}

#[test]
fn a_speculative_pipeline_lands_every_batch_once_in_order_and_never_slower() {
    let h = history();
    let rules = [
        Config::every_ready_rides(COSTS),
        rule(2, 3600, Order::Count),
        rule(4, 7200, Order::Oldest),
    ];
    for c in rules {
        let (seq, spec) = (replay(&h, &c), replay(&h, &speculative(c)));
        let mut ids: Vec<&String> = spec.trains.iter().flat_map(|t| &t.parts).collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), 52, "{c:?}");
        // Main moves one train at a time, so it only ever advances to a train whose CI finished.
        assert!(spec.trains.windows(2).all(|w| w[0].land <= w[1].land));
        assert!(spec.trains.iter().all(|t| t.land >= t.start));
        assert!(spec.makespan_secs <= seq.makespan_secs, "{c:?}");
    }
}

/// `cargo test -p pg-train-model --test replay -- --ignored --nocapture` prints the comparison table.
#[test]
#[ignore]
fn print_the_comparison_over_trains_nine_to_sixteen() {
    let h = history();
    let rows = [
        ("every ready part rides", Config::every_ready_rides(COSTS)),
        ("(a) count, target 2, bound 1h", rule(2, 3600, Order::Count)),
        (
            "(b) oldest, target 2, bound 1h",
            rule(2, 3600, Order::Oldest),
        ),
        ("(a) count, target 4, bound 2h", rule(4, 7200, Order::Count)),
        (
            "(b) oldest, target 4, bound 2h",
            rule(4, 7200, Order::Oldest),
        ),
        (
            "(a) count, target 8, bound 4h",
            rule(8, 14400, Order::Count),
        ),
        (
            "(b) oldest, target 8, bound 4h",
            rule(8, 14400, Order::Oldest),
        ),
    ];
    for per_conflict in [0, 900, 3600] {
        println!("cost of one resolved conflict: {} min", per_conflict / 60);
        for (name, c) in rows {
            let c = with_conflict_cost(c, per_conflict);
            let r = replay(&h, &c);
            let s = replay(&h, &speculative(c));
            println!(
                "{name:34} trains {:2} conflicts {:2} longest wait {:5.1} h mean {:5.1} h parts/h {:.2} speculative {:.2}",
                r.trains.len(),
                r.conflicts,
                r.longest_wait_secs as f64 / 3600.0,
                r.mean_wait_secs as f64 / 3600.0,
                r.parts_per_hour,
                s.parts_per_hour
            );
        }
    }
}
