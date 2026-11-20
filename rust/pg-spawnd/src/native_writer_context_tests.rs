use super::*;

/// The layout `plangraph feeder` writes (`feeder::brief`): headings, multi-line criteria.
const FEEDER: &str = "Plan item: plangraph:PATCH-PR\nTitle: Patch the PR\n\nDescription line.\n\nDone criteria:\nBy test: the first thing\nand a second line of criteria\n\n- a third bullet\n\nFiles:\nsrc/a.rs\n\nNotes:\nnote\n\nTask metadata:\n{}\n\nNative writer rules: x\n";
/// The one-line batch layout of a writer brief.
const BATCH: &str = "### plangraph:LE-X: title\n(priority 8, state open)\nWhy: because\nHow: do the thing\nand carefully\nDone criteria: By test: it works. Merged.\n\n### plangraph:LE-Y: next\nDone criteria: other\n";

#[test]
fn feeder_done_criteria_keep_every_line_up_to_the_next_heading() {
    let done = done_criteria(FEEDER).unwrap();
    assert!(done.starts_with("By test: the first thing"), "{done}");
    assert!(done.contains("and a second line of criteria"), "{done}");
    assert!(done.contains("- a third bullet"), "{done}");
    assert!(
        !done.contains("Files:") && !done.contains("src/a.rs"),
        "{done}"
    );
}
#[test]
fn batch_done_criteria_are_the_inline_text_and_stop_at_the_next_item() {
    assert_eq!(
        done_criteria(BATCH).unwrap(),
        "By test: it works. Merged.\n---\nother"
    );
    assert_eq!(item_keys(BATCH), ["plangraph:LE-X", "plangraph:LE-Y"]);
    assert_eq!(item_keys(FEEDER), ["plangraph:PATCH-PR"]);
    assert_eq!(done_criteria("no criteria here"), None);
}
#[test]
fn invariants_come_from_an_invariants_section_or_the_how_text() {
    assert_eq!(invariants(BATCH).unwrap(), "do the thing\nand carefully");
    assert_eq!(invariants(FEEDER), None);
    let named = "Invariants:\nnever push\nnever merge\n\nDone criteria: x\n";
    assert_eq!(invariants(named).unwrap(), "never push\nnever merge");
}
#[test]
fn changed_symbols_are_the_public_names_a_diff_adds() {
    let diff = "+pub fn writer_families(a: u8) {\n+    fn private() {}\n+pub(crate) fn stop_reason() {}\n+pub struct Budget;\n+def admission_timeout():\n+def _hidden():\n+class ProbeBudget:\n-pub fn removed() {}\n+    pub const MAX: u8 = 1;\n";
    assert_eq!(
        changed_symbols(diff),
        [
            "Budget",
            "MAX",
            "ProbeBudget",
            "admission_timeout",
            "stop_reason",
            "writer_families"
        ]
    );
}
#[test]
fn callers_are_file_and_line_of_the_first_uses() {
    let grep = "a.rs:10:fn x() { sym() }\nb.rs:3:sym();\nc.rs:7:sym();\nd.rs:9:sym();\n";
    assert_eq!(
        callers_line("sym", grep).unwrap(),
        "sym: a.rs:10, b.rs:3, c.rs:7"
    );
    assert_eq!(callers_line("sym", ""), None);
}
