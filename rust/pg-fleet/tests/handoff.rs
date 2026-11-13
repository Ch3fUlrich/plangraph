use serde_json::Value;

#[test]
fn python_card_parity() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/handoff.json")).unwrap();
    for c in cases {
        assert_eq!(
            serde_json::json!(pg_fleet::handoff::check_text(c["text"].as_str().unwrap())),
            c["reasons"],
            "{}",
            c["name"]
        );
    }
}

#[test]
fn python_line_separators_and_empty_trailing_line() {
    assert_eq!(pg_fleet::python_lines(""), Vec::<String>::new());
    assert_eq!(
        pg_fleet::python_lines(
            "a\r\nb\rc\u{b}d\u{c}e\u{1c}f\u{1d}g\u{1e}h\u{85}i\u{2028}j\u{2029}k\n"
        ),
        ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"]
    );
    assert_eq!(pg_fleet::python_lines("\n\n"), ["", ""]);
}
