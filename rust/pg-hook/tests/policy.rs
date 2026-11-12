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
    assert_eq!(
        entries
            .iter()
            .filter(|line| **line == "rust/pg-hook/** -> docs/rust-hooks.md")
            .count(),
        1,
        "the lane adds its crate mapping exactly once"
    );
    let hook = entries
        .iter()
        .position(|line| *line == "rust/pg-hook/** -> docs/rust-hooks.md")
        .unwrap();
    let probe = entries
        .iter()
        .position(|line| line.starts_with("rust/pg-probe/**"))
        .unwrap();
    assert!(hook < probe, "insert crate rules in path order");
    assert!(!repo_file("docs/rust-hooks.md").is_empty());
    assert!(
        !entries
            .iter()
            .any(|line| line.starts_with("rust/Cargo.toml") || line.starts_with("rust/Cargo.lock")),
        "shared workspace files must not map to one crate's documentation"
    );
}

#[test]
fn workspace_preserves_sorted_members_from_both_merge_parents() {
    let manifest = repo_file("rust/Cargo.toml");
    let members = manifest
        .lines()
        .find_map(|line| line.strip_prefix("members = ["))
        .unwrap()
        .trim_end_matches(']')
        .split(',')
        .map(|member| member.trim().trim_matches('"'))
        .collect::<Vec<_>>();
    assert!(members.contains(&"pg-hook"));
    assert!(members.contains(&"pg-reap"));
    assert!(
        members.windows(2).all(|pair| pair[0] < pair[1]),
        "workspace members must retain main's sorted order without duplicates: {members:?}"
    );
}
