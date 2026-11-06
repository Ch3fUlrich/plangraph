use pg_worker::phase::phase_branch;

#[test]
fn phase_branch_collision_and_utf8_vectors() {
    // Mirrored in Python: hash original UTF-8, not sanitised text.
    let vectors = [
        ("a/b", "phase/h/e620c3190468cf61"),
        ("a-b", "phase/h/e61ab31904644883"),
        ("a:b", "phase/h/e661911904a01160"),
        ("é", "phase/h/0ac21707b7181e01"),
        ("😀", "phase/h/feff073875020288"),
        ("e\u{301}", "phase/h/c0c9d418ee802e1f"),
    ];
    let mut branches = std::collections::HashSet::new();
    for (key, expected) in vectors {
        let branch = phase_branch(key).unwrap();
        assert!(branches.insert(branch.clone()));
        assert_eq!(branch, expected);
        assert_eq!(phase_branch(key).unwrap(), branch);
    }
    assert_ne!(
        phase_branch("a/b").unwrap(),
        phase_branch("a-b-e620c3190468cf61").unwrap()
    );
}

#[test]
fn phase_branch_sanitizes_and_rejects_invalid_keys() {
    assert_eq!(
        phase_branch("demo:PH-X").unwrap(),
        "phase/h/4095fd20aa975021"
    );
    assert_eq!(phase_branch("a b/c").unwrap(), "phase/h/69f5280885cdde8c");
    assert_eq!(phase_branch("a_b.c-1").unwrap(), "phase/h/173ed1b77acd86bc");
    assert_eq!(
        phase_branch(&"a".repeat(120)).unwrap(),
        "phase/h/a7533435cc9828fd"
    );

    assert!(phase_branch("").is_err());
    assert!(phase_branch(".hidden").is_err());
    assert!(phase_branch("trailing.").is_err());
    assert!(phase_branch("two..dots").is_err());
    assert!(phase_branch("branch.lock").is_err());
    assert!(phase_branch(&"a".repeat(121)).is_err());
}
