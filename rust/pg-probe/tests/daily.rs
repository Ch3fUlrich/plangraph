use pg_probe::{load, Kind};
#[test]
fn daily_inventory_has_forty_rules_and_ten_knowledge_probes() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/daily-probes.jsonl");
    let fixture = std::fs::read(&path).unwrap();
    let rows = load(std::io::Cursor::new(fixture)).unwrap();
    assert_eq!(rows.iter().filter(|p| p.kind == Kind::Rule).count(), 40);
    assert_eq!(
        rows.iter().filter(|p| p.kind == Kind::Knowledge).count(),
        10
    );
    assert!(rows.iter().all(|p| p.planner_proposed));
}
