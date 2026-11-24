//! The Rust reader of the keeper schema version agrees with the registry the keeper itself runs.
#[path = "../../test_support/python.rs"]
mod test_python;

use plangraph_cli::land::keeper_schema;
use std::path::Path;

#[test]
fn the_rust_reader_matches_schema_pg_latest_of_this_checkout() {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let out = test_python::command()
        .env("PYTHONPATH", repo.join("src"))
        .args([
            "-c",
            "from plangraph.keeper import schema_pg; print(schema_pg.LATEST)",
        ])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let latest: u32 = String::from_utf8_lossy(&out.stdout).trim().parse().unwrap();
    let text = std::fs::read_to_string(repo.join(keeper_schema::SCHEMA_PATH)).unwrap();
    assert_eq!(keeper_schema::parse_latest(&text), Ok(latest));
}
