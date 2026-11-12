use std::path::Path;

fn repo_file(path: &str) -> String {
    std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join(path),
    )
    .unwrap()
}

#[test]
fn retired_python_waivers_stay_retired() {
    let allowlist = repo_file("tools/new_code_allowlist.txt");
    for retired in [
        "src/plangraph/fleet_snapshot.py",
        "src/plangraph/spawner/ps_host_doctor.py",
    ] {
        assert!(
            !allowlist
                .lines()
                .any(|line| line.split('|').next().unwrap().trim() == retired),
            "retired upstream Python waivers must not return"
        );
    }
}

#[test]
fn hook_docs_mapping_is_crate_local_and_sorted_at_insertion() {
    let map = repo_file(".docs-map");
    let entries: Vec<_> = map.lines().filter(|line| !line.starts_with('#')).collect();
    let hook = entries
        .iter()
        .position(|line| *line == "rust/pg-hook/** -> docs/rust-hooks.md")
        .unwrap();
    let probe = entries
        .iter()
        .position(|line| line.starts_with("rust/pg-probe/**"))
        .unwrap();
    assert!(hook < probe, "insert crate rules in path order");
    assert!(
        !entries
            .iter()
            .any(|line| line.starts_with("rust/Cargo.toml") || line.starts_with("rust/Cargo.lock")),
        "shared workspace files must not map to one crate's documentation"
    );
}
