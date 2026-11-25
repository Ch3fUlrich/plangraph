//! `plangraph quota pool`: how stored observation times rank, whatever form they are in.
#[path = "quota_pool/fixtures.rs"]
mod fixtures;

use fixtures::*;

#[test]
fn quota_pool_ranks_numeric_and_iso_timestamps_of_one_instant_alike() {
    let t = now() - 100.0;
    let reset = now() + 86_400.0;
    let directory = tempfile::tempdir().unwrap();
    // One instant written both ways, with fractions a float holds exactly (.25) and not (.1,
    // .3): the later id wins whichever form it is in.
    let mut inserts = String::new();
    for (i, (fraction, nanos)) in [(0.25, 250_000_000), (0.1, 100_000_000), (0.3, 300_000_000)]
        .into_iter()
        .enumerate()
    {
        let (numeric, text) = (format!("{}", t + fraction), iso(t, nanos, 'T', 9));
        let (a, b, id) = (format!("codex:a{i}"), format!("codex:b{i}"), 4 * i as i64);
        inserts += &insert(id + 1, &a, "NULL", "weekly", "90", &text, reset);
        inserts += &insert(id + 2, &a, "NULL", "weekly", "40", &numeric, reset);
        inserts += &insert(id + 3, &b, "NULL", "weekly", "40", &numeric, reset);
        inserts += &insert(id + 4, &b, "NULL", "weekly", "90", &text, reset);
    }
    let weekly = pool(Some(&database_sql(directory.path(), &inserts)))["windows"][0].clone();
    for i in 0..3 {
        let (a, b) = (format!("a{i}"), format!("b{i}"));
        near(&account(&weekly, &a)["used_pct"], 60.0);
        near(&account(&weekly, &b)["used_pct"], 10.0);
        assert_eq!(
            account(&weekly, &a)["observed_at_ns"],
            account(&weekly, &b)["observed_at_ns"]
        );
    }
}

#[test]
fn quota_pool_ranks_parseable_out_of_range_observations_before_eligibility() {
    let t = now() - 100.0;
    let reset = now() + 86_400.0;
    let directory = tempfile::tempdir().unwrap();
    let inserts = [
        insert(1, "codex:a", "NULL", "weekly", "50", &t.to_string(), reset),
        // A parseable future row is the newest reading of its account: that account is not
        // usable, and its older fresh row must not speak for it.
        insert(
            2,
            "codex:a",
            "NULL",
            "weekly",
            "50",
            "'9999-12-31T00:00:00Z'",
            reset,
        ),
        insert(3, "codex:b", "NULL", "weekly", "20", &t.to_string(), reset),
        insert(4, "codex:c", "NULL", "weekly", "50", &t.to_string(), reset),
        insert(5, "codex:c", "NULL", "weekly", "99", "1e300", reset),
        // An absurdly old row is older than the fresh one, which stays the newest.
        insert(6, "codex:d", "NULL", "weekly", "70", &t.to_string(), reset),
        insert(7, "codex:d", "NULL", "weekly", "1", "-1e100", reset),
    ]
    .concat();
    let report = pool(Some(&database_sql(directory.path(), &inserts)));
    let weekly = window(&report, "weekly");
    assert_eq!(weekly["coverage"]["observed_accounts"], 4, "{weekly}");
    assert_eq!(weekly["coverage"]["usable_accounts"], 2, "{weekly}");
    near(&account(weekly, "b")["used_pct"], 80.0);
    near(&account(weekly, "d")["used_pct"], 30.0);
    near(&weekly["used_pct"], 55.0); // b and d only
}

#[test]
fn quota_pool_keeps_the_decimal_of_a_millisecond_timestamp_through_decoding() {
    let t = now() - 100.0;
    let reset = now() + 86_400.0;
    // 0.0015 ms is 1500 ns; a default JSON number decoder moves such a value by 200 ns.
    let numeric = format!("{}.0015", t as i64 * 1_000);
    let text = iso(t, 1_500, 'T', 9);
    let directory = tempfile::tempdir().unwrap();
    let inserts = [
        insert(1, "codex:a", "NULL", "weekly", "90", &text, reset),
        insert(2, "codex:a", "NULL", "weekly", "40", &numeric, reset),
        insert(3, "codex:b", "NULL", "weekly", "40", &numeric, reset),
        insert(4, "codex:b", "NULL", "weekly", "90", &text, reset),
    ]
    .concat();
    let weekly = pool(Some(&database_sql(directory.path(), &inserts)))["windows"][0].clone();
    near(&account(&weekly, "a")["used_pct"], 60.0);
    near(&account(&weekly, "b")["used_pct"], 10.0);
    for name in ["a", "b"] {
        let at = account(&weekly, name)["observed_at_ns"].as_i64().unwrap();
        assert_eq!(at, t as i64 * 1_000_000_000 + 1_500, "{name}");
    }
}
