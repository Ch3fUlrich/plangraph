use super::*;
#[test]
fn binary_and_multiline_secret_detected() {
    let mut bytes = vec![255, 0];
    bytes.extend(["gh", "p_", &"a".repeat(25)].concat().bytes());
    assert!(scan_bytes(&bytes));
    let pem = [
        "-----BEGIN ",
        "PRIVATE KEY-----\nbody\n-----END ",
        "PRIVATE KEY-----",
    ]
    .concat();
    assert!(scan_bytes(pem.as_bytes()));
    assert!(!scan_bytes(b"safe text"));
}
