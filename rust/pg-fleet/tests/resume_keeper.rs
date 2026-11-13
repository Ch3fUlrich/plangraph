use pg_fleet::digest::{Config, State};
use serde_json::{json, Value};

fn item(key: &str, state: &str, holder: &str) -> Value {
    json!({"key":key,"state":state,"claimed_by":holder,"project":"plangraph","kind":"lane","priority":2,"updated_at":"2026-10-31T07:30:00Z"})
}
#[test]
fn recorded_principal_claims_never_false_lapsed() {
    let d = tempfile::tempdir().unwrap();
    let mut c = Config::isolated(d.path());
    c.principal = Some("plangraph-L1-main".into());
    let mut s = State::fixture(c, "build-L1");
    s.card_text="- claimed: MIMO-REASONING-ONLY-AFTER-781 (claimed), WIN-ATOMIC-WRITE-RENAME-EX (in_review), OTHER, EXPIRED".into();
    let mut held = item(
        "plangraph:MIMO-REASONING-ONLY-AFTER-781",
        "claimed",
        "plangraph-L1-main",
    );
    held["lease_until"] = json!("2026-10-31T09:40:00Z");
    let mut expired = item("plangraph:EXPIRED", "claimed", "plangraph-L1-main");
    expired["lease_until"] = json!("2026-10-31T07:00:00Z");
    let snap = json!({"generated":"2026-10-31T07:58:00Z","items":[held,item("plangraph:WIN-ATOMIC-WRITE-RENAME-EX","in_review","plangraph-L1-main"),item("plangraph:OTHER","claimed","another-principal"),expired]});
    std::fs::write(&s.config.snapshot, snap.to_string()).unwrap();
    let out = pg_fleet::digest_keeper::section(&mut s).join("\n");
    assert!(out.contains("MIMO-REASONING-ONLY-AFTER-781 claimed"));
    assert!(out.contains("WIN-ATOMIC-WRITE-RENAME-EX in_review"));
    assert!(!out.contains("LAPSED MIMO"));
    assert!(!out.contains("LAPSED WIN"));
    assert!(out.contains("LAPSED OTHER"));
    assert!(out.contains("LAPSED EXPIRED"));
}

#[test]
fn suffix_collisions_duplicate_lapses_and_unknown_keys() {
    for reverse in [false, true] {
        for key in ["X", "p:X", "q:X"] {
            let d = tempfile::tempdir().unwrap();
            let mut s = State::fixture(Config::isolated(d.path()), "w");
            let mut items = vec![item("p:X", "claimed", "w"), item("q:X", "open", "other")];
            if reverse {
                items.reverse();
            }
            std::fs::write(&s.config.snapshot, json!({"items":items}).to_string()).unwrap();
            s.card_text = format!("- claimed: {key}, {key}\n- claimed: {key}");
            let rows = pg_fleet::digest_keeper::section(&mut s);
            assert_eq!(
                rows.iter().filter(|s| s.starts_with("LAPSED")).count(),
                usize::from(key == "q:X")
            );
        }
    }
    for state in ["claimed", "in_review"] {
        for key in ["HELD", "plangraph:HELD"] {
            let d = tempfile::tempdir().unwrap();
            let mut s = State::fixture(Config::isolated(d.path()), "w");
            std::fs::write(
                &s.config.snapshot,
                json!({"items":[item("plangraph:HELD",state,"w")]}).to_string(),
            )
            .unwrap();
            s.card_text = format!("- claimed: {key} (claimed, since yesterday)");
            assert!(!pg_fleet::digest_keeper::section(&mut s)
                .iter()
                .any(|s| s.starts_with("LAPSED")));
        }
    }
}
