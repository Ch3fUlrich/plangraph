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
const RUST_AT_HEAD: &str = "use x;\n\n/// Doc.\npub fn writer_families(a: u8) {\n    let b = 1;\n    fn private() {\n        let c = 2;\n    }\n}\n\nfn hidden() {\n    let d = 3;\n}\n\npub struct Budget;\n";
const PY_AT_HEAD: &str = "def _hidden():\n    return 1\n\nclass ProbeBudget:\n    def timeout(self):\n        return 2\n\nasync def admit_base(a):\n    return 3\n";

#[test]
fn changed_symbols_are_the_enclosing_public_definitions_of_every_changed_line() {
    let read = |path: &str| match path {
        "a.rs" => Some(RUST_AT_HEAD.to_owned()),
        "b.py" => Some(PY_AT_HEAD.to_owned()),
        _ => None,
    };
    // A changed body counts, as does an added declaration; private scopes and non-source files do not.
    let diff = "+++ b/a.rs\n@@ -5,0 +5,1 @@ x\n+    let b = 1;\n@@ -7,1 +7,1 @@\n-old\n+        let c = 2;\n@@ -12,1 +12,1 @@\n+    let d = 3;\n@@ -15,0 +15,1 @@\n+pub struct Budget;\n+++ b/b.py\n@@ -2,1 +2,1 @@\n+    return 1\n@@ -6,1 +6,1 @@\n+        return 2\n@@ -9,1 +9,1 @@\n+    return 3\n+++ b/notes.md\n@@ -1,1 +1,1 @@\n+text\n";
    assert_eq!(
        changed_symbols(diff, &read),
        ["Budget", "admit_base", "timeout", "writer_families"]
    );
    assert!(changed_symbols("+++ b/gone.rs\n@@ -1,1 +1,1 @@\n+x\n", &read).is_empty());
}
#[test]
fn closed_scopes_do_not_own_later_lines() {
    let rust = "pub struct S;\nimpl S {\n    pub fn method(&self) {\n        let a = 1;\n    }\n}\nconst LATER: u8 = 1;\n";
    // Line 4 is inside `method`; line 7 follows the closed impl.
    assert_eq!(enclosing_public(rust, 4, false).as_deref(), Some("method"));
    assert_eq!(enclosing_public(rust, 7, false), None);
    let python = "def early():\n    return 1\n\nif FLAG:\n    VALUE = 2\n\nclass K:\n    def m(self):\n        pass\n";
    assert_eq!(enclosing_public(python, 5, true), None);
    assert_eq!(enclosing_public(python, 9, true).as_deref(), Some("m"));
    // A wrapped signature does not close its own scope.
    let wrapped = "pub fn long(\n    a: u8,\n) -> u8 {\n    a\n}\n";
    assert_eq!(enclosing_public(wrapped, 4, false).as_deref(), Some("long"));
}
#[test]
fn callers_are_code_uses_not_definitions_comments_strings_or_declarations() {
    let grep = "docs.md:1:sym is documented\na.rs:2:/// sym docs\na.rs:3:// sym comment\na.rs:4:pub fn sym() {}\nb.py:5:def sym(a):\nb.py:6:# sym again\ng.rs:7:pub mod sym;\ng.rs:8:let key = \"sym\";\ng.rs:9:json!({\"sym\": 1});\nc.rs:10:    sym();\nd.py:11:x = sym(1)\ne.rs:12:let a = crate::sym::sym(2);\nf.rs:13:sym();\n";
    assert_eq!(
        callers_line("sym", grep).unwrap(),
        "sym: c.rs:10, d.py:11, e.rs:12"
    );
    assert_eq!(
        callers_line("sym", "a.rs:4:pub fn sym() {}\na.rs:5:pub mod sym;\n"),
        None
    );
}
