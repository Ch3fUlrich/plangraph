use super::*;

#[test]
fn concurrent_key_and_release_on_unwind() {
    let cfg = Config::default();
    let key = "pilot-owner-test";
    let first = enter(&cfg, key).unwrap();
    assert!(enter(&cfg, key).err().unwrap().to_string().contains(key));
    drop(first);
    assert!(
        std::panic::catch_unwind(|| {
            let _owner = enter(&cfg, key).unwrap();
            panic!("test unwind");
        })
        .is_err()
    );
    assert!(enter(&cfg, key).is_ok());
}

#[test]
fn admission_and_no_claim_are_refusals() {
    let cfg = Config {
        card_allowlist: Some(["listed".to_owned()].into()),
        ..Config::default()
    };
    assert!(allow(&cfg, "listed").is_ok());
    assert!(allow(&cfg, "unlisted").is_err());
    assert!(claimed(None, "listed").is_err());
}
