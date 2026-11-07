//! Source-level guard supplements the public-API doctest even under `--lib`.

#[test]
fn mock_module_and_export_remain_feature_gated() {
    let source = include_str!("io.rs");
    let lines: Vec<_> = source.lines().map(str::trim).collect();
    for declaration in [
        "mod mock_transport;",
        "pub use mock_transport::MockTransport;",
    ] {
        let index = lines.iter().position(|line| *line == declaration).unwrap();
        let mut attributes = index;
        while attributes > 0 && lines[attributes - 1].starts_with("#[") {
            attributes -= 1;
        }
        assert!(
            lines[attributes..index].contains(&"#[cfg(any(test, feature = \"mock\"))]"),
            "{declaration} must not expose the mock in a default library build"
        );
    }
}
