use super::*;

fn writer(intelligence: Option<f64>, window: Option<u64>) -> Writer {
    Writer {
        aa_intelligence: intelligence,
        context_window: window,
    }
}
fn item(key: &str, kind: &str, bytes: Option<u64>) -> Item {
    Item {
        key: key.into(),
        kind: kind.into(),
        expected_bytes: bytes,
    }
}
fn input(writer: Writer, sizes: &[u64]) -> Input {
    Input {
        writer,
        items: sizes
            .iter()
            .enumerate()
            .map(|(i, b)| item(&format!("k{i}"), "lane", Some(*b)))
            .collect(),
        merged_bytes: BTreeMap::new(),
        limit_bytes: None,
        last: None,
    }
}

#[test]
fn model_cap_is_10_5_or_3() {
    assert_eq!(model_cap(&writer(Some(60.0), Some(400_000))).0, 10);
    assert_eq!(model_cap(&writer(Some(50.0), Some(1_000_000))).0, 10);
    assert_eq!(model_cap(&writer(Some(60.0), Some(399_999))).0, 5);
    assert_eq!(model_cap(&writer(Some(60.0), None)).0, 5);
    assert_eq!(model_cap(&writer(Some(49.9), Some(1_000_000))).0, 3);
    assert_eq!(model_cap(&writer(None, Some(1_000_000))).0, 3);
}

#[test]
fn small_items_are_limited_by_the_model_cap() {
    let sizes = [1_000; 12];
    assert_eq!(
        plan(&input(writer(Some(60.0), Some(500_000)), &sizes)).count,
        10
    );
    assert_eq!(
        plan(&input(writer(Some(60.0), Some(200_000)), &sizes)).count,
        5
    );
    assert_eq!(
        plan(&input(writer(Some(10.0), Some(500_000)), &sizes)).count,
        3
    );
}

#[test]
fn sizes_over_the_planning_target_give_a_smaller_count() {
    // Target is 60% of the 100k limit; 4 x 14k = 56k fits, the fifth passes 60k.
    let p = plan(&input(writer(Some(60.0), Some(500_000)), &[14_000; 8]));
    assert_eq!(p.count, 4);
    assert!(
        p.reason.contains("k4 would pass the target"),
        "{}",
        p.reason
    );
}

#[test]
fn an_item_above_half_the_limit_goes_alone() {
    let first = plan(&input(
        writer(Some(60.0), Some(500_000)),
        &[60_000, 1_000, 1_000],
    ));
    assert_eq!(
        (first.count, first.keys.as_slice()),
        (1, ["k0".to_string()].as_slice())
    );
    let later = plan(&input(
        writer(Some(60.0), Some(500_000)),
        &[1_000, 70_000, 1_000],
    ));
    assert_eq!(later.keys, ["k0", "k2"]);
    assert_eq!(later.alone, ["k1"]);
    // Exactly half still shares.
    assert_eq!(
        plan(&input(writer(Some(60.0), Some(500_000)), &[50_000]))
            .alone
            .len(),
        0
    );
}

#[test]
fn feedback_moves_the_count_by_one() {
    let sizes = [1_000; 12];
    let mut base = input(writer(Some(60.0), Some(500_000)), &sizes);
    base.last = Some(Last {
        count: 6,
        outcome: Outcome::Skipped,
    });
    assert_eq!(plan(&base).count, 5);
    base.last = Some(Last {
        count: 6,
        outcome: Outcome::HighSurvived,
    });
    assert_eq!(plan(&base).count, 5);
    base.last = Some(Last {
        count: 6,
        outcome: Outcome::AcceptedRound1,
    });
    assert_eq!(plan(&base).count, 7);
    base.last = Some(Last {
        count: 6,
        outcome: Outcome::Neutral,
    });
    assert_eq!(plan(&base).count, 6);
    // One more never passes the model cap.
    base.last = Some(Last {
        count: 10,
        outcome: Outcome::AcceptedRound1,
    });
    assert_eq!(plan(&base).count, 10);
    // One fewer never reaches zero.
    base.last = Some(Last {
        count: 1,
        outcome: Outcome::Skipped,
    });
    assert_eq!(plan(&base).count, 1);
}

#[test]
fn missing_estimates_use_the_median_of_merged_items_of_the_kind() {
    let merged: BTreeMap<String, Vec<u64>> = [
        ("lane".to_string(), vec![10_000, 30_000, 20_000]),
        ("goal".to_string(), vec![1_000]),
    ]
    .into();
    assert_eq!(expected_bytes(&item("a", "lane", None), &merged), 20_000);
    assert_eq!(
        expected_bytes(&item("a", "gate", None), &merged),
        median(&[1_000, 10_000, 20_000, 30_000]).unwrap()
    );
    assert_eq!(expected_bytes(&item("a", "lane", Some(5)), &merged), 5);
    assert_eq!(
        expected_bytes(&item("a", "lane", None), &BTreeMap::new()),
        UNKNOWN_ITEM_BYTES
    );
}

#[test]
fn the_reason_names_every_bound_and_the_json_input_parses() {
    let json = r#"{"writer":{"aa_intelligence":60,"context_window":500000},
        "items":[{"key":"a","kind":"lane","expected_bytes":5000}],
        "merged_bytes":{"lane":[1,2,3]}, "last":{"count":3,"outcome":"accepted_round1"}}"#;
    let parsed: Input = serde_json::from_str(json).unwrap();
    let p = plan(&parsed);
    assert_eq!(p.count, 1);
    for part in ["model cap 10", "feedback 4", "fit 5000 of 60000"] {
        assert!(p.reason.contains(part), "{part} in {}", p.reason);
    }
}
