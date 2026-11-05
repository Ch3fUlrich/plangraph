use super::*;
use serde_json::json;

fn fixture() -> (Policy, Input, Limits, Authority) {
    let proof = json!({"id":"proof","observed_at":0,"provenance":"fixture"});
    let identity = json!({"role":"judge","model":"synthetic","harness":"fake"});
    let seat = Seat {
        identity: identity.clone(),
        model: "synthetic".into(),
        protocol: "json".into(),
        canonical: "one".into(),
        route_family: Some("outside".into()),
        route_observed_at: 0,
        route_expires_at: 9999999,
        route_provenance: "route".into(),
        catalog_provenance: "catalog".into(),
        proving_run: proof.clone(),
        claimed_family: Some("writer".into()),
        witnessed_family: None,
        header: vec![],
        mandatory_no_tools_header: None,
        judge_only: false,
        judge_forbidden_writer_families: BTreeSet::new(),
        unavailable: true,
        probe: Probe {
            successful: false,
            void: false,
            timeout: false,
            authorization_reference: String::new(),
        },
    };
    let catalog = json!({"schema_version":1,"observed_at":0,"entries":[{
        "identity":identity,"base":{"window":{"Known":100},"unit":"tokens",
        "observed_at":0,"expires_at":9999999,"provenance":"catalog"},
        "served":[{"model":"synthetic","protocol":"json","family":"claimed",
        "proving_run":proof}]}]});
    let mut other = seat.clone();
    other.canonical = "two".into();
    other.route_family = Some("another".into());
    (
        Policy {
            quorum: 1,
            sha: "a".repeat(40),
            reserved_gpt_family: "reserved".into(),
            writer_families: BTreeSet::from(["writer".into()]),
            disallowed_pairs: BTreeSet::new(),
            mandatory_no_tools_header: None,
            judge_only: false,
            judge_forbidden_writer_families: BTreeSet::new(),
        },
        Input {
            epoch_day: 0,
            now_s: 10,
            monotonic_ms: 10,
            previous_monotonic_ms: 0,
            catalog_bytes: serde_json::to_vec(&catalog).unwrap(),
            seats: vec![seat, other],
            ruling: None,
        },
        Limits {
            daily_attempts: 2,
            timeout_s: 5,
            retry_cap: 1,
        },
        Authority {
            reference: "approval".into(),
            day: 0,
            budget: 2,
            timeout_s: 5,
            retry_cap: 1,
            existing_probe_access: BTreeSet::from(["one".into(), "two".into()]),
            may_record_not_needed: true,
        },
    )
}

#[test]
fn replay_restore_days_and_clocks() {
    let (p, mut i, l, mut a) = fixture();
    let mut s = State::default();
    let mut requests = vec![];
    plan(&mut s, &p, &i, &l, Some(&a), &mut |r| requests.push(r));
    s = serde_json::from_slice(&serde_json::to_vec(&s).unwrap()).unwrap();
    plan(&mut s, &p, &i, &l, Some(&a), &mut |r| requests.push(r));
    assert_eq!(requests.len(), 1);
    for day in [1, 8] {
        i.epoch_day = day;
        i.now_s = day * 86400;
        i.monotonic_ms += 1;
        a.day = day;
        plan(&mut s, &p, &i, &l, Some(&a), &mut |r| requests.push(r));
        assert_eq!(s.attempts.len(), 1);
        assert_eq!(s.day, Some(day));
    }
    assert_eq!(requests.len(), 3);
    i.now_s -= 1;
    assert_eq!(
        plan(&mut s, &p, &i, &l, Some(&a), &mut |_| panic!()),
        vec![Record::Finding("clock_backwards_or_invalid")]
    );
    assert_eq!(s.day, Some(8));
}

#[test]
fn void_retry_timeout_and_unavailable() {
    let (p, i, l, a) = fixture();
    for timeout in [false, true] {
        let mut s = State::default();
        plan(&mut s, &p, &i, &l, Some(&a), &mut |_| {});
        assert!(complete(
            &mut s,
            1,
            if timeout {
                Outcome::Timeout
            } else {
                Outcome::Void
            }
        ));
        assert!(!complete(&mut s, 1, Outcome::Success));
        plan(&mut s, &p, &i, &l, Some(&a), &mut |_| {});
        assert_eq!(s.attempts.len(), if timeout { 1 } else { 2 });
        if !timeout {
            assert_ne!(
                s.attempts[0].seat.route_family,
                s.attempts[1].seat.route_family
            );
            complete(&mut s, 2, Outcome::Void);
            assert_eq!(
                plan(&mut s, &p, &i, &l, Some(&a), &mut |_| panic!()),
                vec![Record::Finding("attempt_or_retry_limit")]
            );
        }
        assert!(s.retained.values().all(|s| !s.probe.successful));
    }
    let mut s = State::default();
    let mut i = i;
    i.seats[1].route_family = Some("writer".into());
    plan(&mut s, &p, &i, &l, Some(&a), &mut |_| {});
    complete(&mut s, 1, Outcome::Void);
    assert_eq!(
        plan(&mut s, &p, &i, &l, Some(&a), &mut |_| panic!()),
        vec![Record::Finding("eligible_family_unavailable")]
    );
}

#[test]
fn authority_not_needed_and_no_external_effects() {
    let (p, i, l, a) = fixture();
    let fake_fs = BTreeMap::from([("catalog", i.catalog_bytes.clone())]);
    let before = fake_fs.clone();
    for case in 0..7 {
        let mut a = a.clone();
        match case {
            1 => a.reference.clear(),
            2 => a.budget = 1,
            3 => a.timeout_s = 1,
            4 => a.retry_cap = 0,
            5 => a.existing_probe_access.clear(),
            6 => a.day = 1,
            _ => (),
        }
        let mut s = State::default();
        let records = plan(
            &mut s,
            &p,
            &i,
            &l,
            (case != 0).then_some(&a),
            &mut |_| panic!(),
        );
        assert!(records.contains(&Record::RequiresOperatorAuthorization));
        assert!(s.attempts.is_empty());
    }
    let mut s = State::default();
    plan(&mut s, &p, &i, &l, Some(&a), &mut |r| {
        assert!(r.authorized);
        assert_eq!((r.timeout_s, r.retry_cap), (5, 1));
    });
    complete(&mut s, 1, Outcome::Success);
    assert!(
        matches!(&plan(&mut s, &p, &i, &l, Some(&a), &mut |_| panic!())[..],
        [Record::NotNeeded { authority_reference, reason }] if authority_reference == "approval" && !reason.is_empty())
    );
    assert_eq!(fake_fs, before);
    assert_eq!(i.catalog_bytes, before["catalog"]);
}
