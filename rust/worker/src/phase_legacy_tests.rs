use super::legacy_phase_branch;

#[test]
fn legacy_names_are_pinned() {
    for (key, expected) in [
        ("demo:PH-X", "phase/demo-PH-X"),
        ("a/b", "phase/a-b"),
        ("a-b", "phase/a-b"),
        ("é", "phase/-"),
        ("😀", "phase/-"),
        ("e\u{301}", "phase/e-"),
        ("a_b.c-1", "phase/a_b.c-1"),
    ] {
        assert_eq!(legacy_phase_branch(key).unwrap(), expected);
    }
    assert_eq!(
        legacy_phase_branch(&"a".repeat(120)).unwrap(),
        format!("phase/{}", "a".repeat(120))
    );
    for key in [
        "",
        ".hidden",
        "trailing.",
        "two..dots",
        "branch.lock",
        &"a".repeat(121),
    ] {
        assert!(legacy_phase_branch(key).is_err(), "{key}");
    }
}
