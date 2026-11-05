use super::*;
use serde_json::json;

fn fixture() -> (Policy, Input) {
    let proof = json!({"id":"run","observed_at":1,"provenance":"proof"});
    let identity = json!({"role":"judge","model":"synthetic","harness":"fixture"});
    let catalog = json!({"schema_version":1,"observed_at":1,"entries":[{
        "identity":identity,"base":{"window":{"Known":100},"unit":"tokens",
        "observed_at":1,"expires_at":100,"provenance":"catalog"},
        "served":[{"model":"synthetic","protocol":"json","family":"claimed",
        "proving_run":proof}]}]});
    let seat = Seat {
        identity,
        model: "synthetic".into(),
        protocol: "json".into(),
        canonical: "seat".into(),
        route_family: Some("independent".into()),
        route_observed_at: 1,
        route_expires_at: 100,
        route_provenance: "route".into(),
        catalog_provenance: "catalog".into(),
        proving_run: proof,
        claimed_family: Some("claimed".into()),
        witnessed_family: None,
        header: b"NO TOOLS\r\nold\0".to_vec(),
        mandatory_no_tools_header: Some(b"NO TOOLS\r\n".to_vec()),
        judge_only: true,
        judge_forbidden_writer_families: BTreeSet::from(["same".into()]),
        unavailable: false,
        probe: Probe {
            successful: true,
            void: false,
            timeout: false,
            authorization_reference: "".into(),
        },
    };
    (
        Policy {
            quorum: 1,
            sha: "a".repeat(40),
            reserved_gpt_family: "gpt".into(),
            writer_families: BTreeSet::from(["writer-a".into(), "writer-b".into()]),
            disallowed_pairs: BTreeSet::new(),
            mandatory_no_tools_header: Some(b"NO TOOLS\r\n".to_vec()),
            judge_only: true,
            judge_forbidden_writer_families: BTreeSet::from(["same".into()]),
        },
        Input {
            epoch_day: 0,
            now_s: 10,
            monotonic_ms: 2,
            previous_monotonic_ms: 1,
            catalog_bytes: serde_json::to_vec(&catalog).unwrap(),
            seats: vec![seat],
            ruling: None,
        },
    )
}
#[test]
fn provenance_and_missing_witness() {
    let (p, i) = fixture();
    let r = count(&p, &i);
    assert!(!r.hold);
    assert!(r.readings[0].findings.contains("missing_witness"));
    assert!(r.readings[0].findings.contains("family_disagreement"));
    for case in 0..8 {
        let mut bad = i.clone();
        match case {
            0 => bad.catalog_bytes.clear(),
            1 => bad.catalog_bytes = b"{".to_vec(),
            2 => bad.now_s = 100,
            3 => bad.seats[0].protocol = "other".into(),
            4 => bad.seats[0].catalog_provenance = "other".into(),
            5 => bad.seats[0].proving_run["id"] = json!("other"),
            6 => bad.seats[0].route_family = None,
            _ => bad.seats[0].route_provenance.clear(),
        }
        let r = count(&p, &bad);
        assert!(r.hold, "case {case}");
        assert!(r.readings[0]
            .findings
            .contains("unknown_catalog_or_route_evidence"));
    }
}
#[test]
fn exclusions_aliases_and_pairs() {
    let (mut p, mut i) = fixture();
    for family in ["gpt", "writer-a", "writer-b"] {
        i.seats[0].route_family = Some(family.into());
        i.seats[0].witnessed_family = Some("independent".into());
        assert!(count(&p, &i).hold);
    }
    i.seats[0].route_family = Some("independent".into());
    let mut alias = i.seats[0].clone();
    alias.route_family = Some("another".into());
    i.seats.push(alias);
    p.quorum = 2;
    assert_eq!(count(&p, &i).families.len(), 1);
    p.disallowed_pairs
        .insert(("writer-b".into(), "seat".into()));
    assert!(count(&p, &i).families.is_empty());
}
#[test]
fn header_judge_and_unavailable() {
    let (mut p, mut i) = fixture();
    let original = i.seats[0].header.clone();
    assert_eq!(count(&p, &i).readings[0].seat.header, original);
    i.seats[0].header.clear();
    assert!(count(&p, &i).hold);
    i.seats[0].header = original;
    p.writer_families.insert("same".into());
    assert!(count(&p, &i).hold);
    p.writer_families.remove("same");
    i.seats[0].unavailable = true;
    assert!(count(&p, &i).readings[0].disabled);
    i.seats[0].probe.authorization_reference = "authorized".into();
    assert!(!count(&p, &i).hold);
    i.seats[0].probe.void = true;
    assert!(count(&p, &i).readings[0].disabled);
}
#[test]
fn shortage_ruling_and_results() {
    let (mut p, mut i) = fixture();
    p.quorum = 2;
    i.ruling = Some(Ruling {
        decision_reference: "D-1".into(),
        sha: p.sha.clone(),
        reason: "recorded".into(),
    });
    let r = count(&p, &i);
    assert!(r.hold && r.ruling.is_some());
    assert_eq!(r.families.len(), 1);
    assert_eq!(
        r.findings.iter().filter(|f| *f == "router_alarm").count(),
        1
    );
    for case in 0..3 {
        let mut bad = i.clone();
        let ruling = bad.ruling.as_mut().unwrap();
        match case {
            0 => ruling.sha = "b".repeat(40),
            1 => ruling.reason.clear(),
            _ => ruling.decision_reference.clear(),
        }
        let r = count(&p, &bad);
        assert!(r.hold && r.ruling.is_none() && r.findings.contains("invalid_ruling"));
    }
    i.ruling = None;
    p.quorum = 1;
    assert!(!count(&p, &i).hold);
    for case in 0..5 {
        let mut bad = i.clone();
        let mut policy = p.clone();
        match case {
            0 => bad.seats[0].probe.void = true,
            1 => bad.seats[0].probe.timeout = true,
            2 => bad.seats[0].probe.successful = false,
            3 => policy.quorum = 0,
            _ => policy.sha.clear(),
        }
        assert!(count(&policy, &bad).hold);
    }
}
#[test]
fn json_roundtrip() {
    fn roundtrip<T: Serialize + serde::de::DeserializeOwned + PartialEq + std::fmt::Debug>(v: T) {
        assert_eq!(
            serde_json::from_slice::<T>(&serde_json::to_vec(&v).unwrap()).unwrap(),
            v
        );
    }
    let (p, i) = fixture();
    roundtrip(count(&p, &i));
    roundtrip(p);
    roundtrip(i);
}

#[test]
fn policy_requirements_cannot_be_opted_out() {
    let (mut p, mut i) = fixture();
    i.seats[0].mandatory_no_tools_header = None;
    i.seats[0].judge_only = false;
    i.seats[0].judge_forbidden_writer_families.clear();
    assert!(!count(&p, &i).hold);
    let header = std::mem::take(&mut i.seats[0].header);
    assert!(count(&p, &i).hold, "policy header bypass");
    i.seats[0].header = header;
    p.writer_families.insert("same".into());
    assert!(count(&p, &i).hold, "policy judge bypass");
}

#[test]
fn unconsumed_pairs_fail_closed() {
    for (writer, canonical) in [("absent", "seat"), ("writer-a", "absent")] {
        let (mut p, i) = fixture();
        p.disallowed_pairs.insert((writer.into(), canonical.into()));
        let r = count(&p, &i);
        assert!(r.hold && r.families.is_empty());
        assert!(r.findings.contains("unconsumed_policy_pair"));
    }
}
