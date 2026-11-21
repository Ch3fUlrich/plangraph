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
fn func(name: &str) -> Symbol {
    Symbol {
        name: name.into(),
        type_like: false,
    }
}
fn kind(name: &str) -> Symbol {
    Symbol {
        name: name.into(),
        type_like: true,
    }
}
fn owner(source: &str, at: usize, python: bool) -> Option<String> {
    enclosing_public(source, at, python).map(|symbol| symbol.name)
}
fn names(symbols: Vec<Symbol>) -> Vec<String> {
    symbols.into_iter().map(|symbol| symbol.name).collect()
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
        names(changed_symbols(diff, &read)),
        ["Budget", "admit_base", "timeout", "writer_families"]
    );
    assert!(changed_symbols("+++ b/gone.rs\n@@ -1,1 +1,1 @@\n+x\n", &read).is_empty());
}
#[test]
fn closed_scopes_do_not_own_later_lines() {
    let rust = "pub struct S;\nimpl S {\n    pub fn method(&self) {\n        let a = 1;\n    }\n}\nconst LATER: u8 = 1;\n";
    // Line 4 is inside `method`; line 7 follows the closed impl.
    assert_eq!(owner(rust, 4, false).as_deref(), Some("method"));
    assert_eq!(owner(rust, 7, false), None);
    let python = "def early():\n    return 1\n\nif FLAG:\n    VALUE = 2\n\nclass K:\n    def m(self):\n        pass\n";
    assert_eq!(owner(python, 5, true), None);
    assert_eq!(owner(python, 9, true).as_deref(), Some("m"));
    // A wrapped signature does not close its own scope.
    let wrapped = "pub fn long(\n    a: u8,\n) -> u8 {\n    a\n}\n";
    assert_eq!(owner(wrapped, 4, false).as_deref(), Some("long"));
}
#[test]
fn callers_are_code_uses_not_definitions_comments_strings_or_declarations() {
    let grep = "docs.md:1:sym is documented\na.rs:2:/// sym docs\na.rs:3:// sym comment\na.rs:4:pub fn sym() {}\nb.py:5:def sym(a):\nb.py:6:# sym again\ng.rs:7:pub mod sym;\ng.rs:8:let key = \"sym\";\ng.rs:9:json!({\"sym\": 1});\nc.rs:10:    sym();\nd.py:11:x = sym(1)\ne.rs:12:let a = crate::sym::sym(2);\nf.rs:13:sym();\n";
    assert_eq!(
        callers_line(&func("sym"), grep).unwrap(),
        "sym: c.rs:10, d.py:11, e.rs:12"
    );
    assert_eq!(
        callers_line(
            &func("sym"),
            "a.rs:4:pub fn sym() {}\na.rs:5:pub mod sym;\n"
        ),
        None
    );
}
#[test]
fn the_first_line_after_a_closed_definition_belongs_to_none_of_it() {
    // A module-level assignment right after a function body.
    let python = "def early():\n    return 1\nVALUE = 2\n\ndef later():\n    return 3\n";
    assert_eq!(owner(python, 2, true).as_deref(), Some("early"));
    assert_eq!(owner(python, 3, true), None);
    // An `impl` header and a blank line after a one-line function.
    let rust = "pub fn one() {}\nimpl S {\n    fn m() {\n        let a = 1;\n    }\n}\n\npub fn two() {\n    let b = 2;\n\n    let c = 3;\n}\n";
    assert_eq!(owner(rust, 2, false), None);
    assert_eq!(owner(rust, 7, false), None);
    // The closing brace and an inner blank line stay with the function they belong to.
    assert_eq!(owner(rust, 12, false).as_deref(), Some("two"));
    assert_eq!(owner(rust, 10, false).as_deref(), Some("two"));
}
#[test]
fn module_path_uses_do_not_displace_real_calls_under_the_limit() {
    let grep = "a.rs:1:    let a = pg::writer_families::python_command(s);\na.rs:2:    pg::writer_families::render_review_brief(c);\nb.rs:3:    pg::writer_families::writer_families(x)?;\nc.rs:4:    writer_families(y);\nd.rs:5:    writer_families(z);\ne.rs:6:    writer_families::<u8>(z);\n";
    assert_eq!(
        callers_line(&func("writer_families"), grep).unwrap(),
        "writer_families: b.rs:3, c.rs:4, d.rs:5"
    );
    // A type name followed by a path is a use of the type; a lowercase module path is not.
    assert_eq!(
        callers_line(&kind("Budget"), "a.rs:1:let b = Budget::new();\n").unwrap(),
        "Budget: a.rs:1"
    );
    assert_eq!(
        callers_line(&func("only_mod"), "a.rs:1:only_mod::f();\n"),
        None
    );
}
#[test]
fn where_is_a_keyword_not_a_prefix() {
    let python = "def early():\n    return 1\nwhere_used = 2\n";
    assert_eq!(owner(python, 3, true), None);
    let rust = "pub fn long<T>(\n    a: T,\n) -> u8\nwhere\n    T: Copy,\n{\n    1\n}\n";
    assert_eq!(owner(rust, 4, false).as_deref(), Some("long"));
}
#[test]
fn a_blank_line_belongs_to_the_block_it_sits_in() {
    // Before the closing brace the blank line is still in the body; after a body it is not.
    let rust = "pub fn two() {\n    let b = 2;\n\n}\n\nfn other() {}\n";
    assert_eq!(owner(rust, 3, false).as_deref(), Some("two"));
    assert_eq!(owner(rust, 5, false), None);
    let python = "def early():\n    return 1\n\ndef later():\n    return 2\n";
    assert_eq!(owner(python, 3, true), None);
    let inner = "def early():\n    a = 1\n\n    return 1\n";
    assert_eq!(owner(inner, 3, true).as_deref(), Some("early"));
}
#[test]
fn a_lowercase_type_is_used_through_its_path_and_the_kind_comes_from_the_declaration() {
    let source = "pub enum status_code {\n    Ok,\n}\n\npub fn render() {\n    let a = 1;\n}\n";
    let diff = "+++ b/a.rs\n@@ -2,1 +2,1 @@\n+    Ok,\n@@ -6,1 +6,1 @@\n+    let a = 1;\n";
    let symbols = changed_symbols(diff, &|_| Some(source.to_owned()));
    assert_eq!(
        symbols,
        [
            Symbol {
                name: "render".into(),
                type_like: false
            },
            Symbol {
                name: "status_code".into(),
                type_like: true
            },
        ]
    );
    let grep = "a.rs:1:let s = status_code::Ok;\nb.rs:2:let r = render::helper();\n";
    assert_eq!(
        callers_line(&symbols[1], grep).unwrap(),
        "status_code: a.rs:1"
    );
    assert_eq!(callers_line(&symbols[0], grep), None);
}
#[test]
fn where_continues_a_signature_only_in_rust() {
    let python = "def early():\n    return 1\nwhere = 2\n";
    assert_eq!(owner(python, 3, true), None);
    let rust = "pub fn long<T>(a: T)\nwhere\n    T: Copy,\n{\n    1\n}\n";
    assert_eq!(owner(rust, 2, false).as_deref(), Some("long"));
}
#[test]
fn a_blank_line_after_an_opening_line_is_inside_its_block() {
    let empty = "pub fn empty() {\n\n}\n";
    assert_eq!(owner(empty, 2, false).as_deref(), Some("empty"));
    let python = "def early():\n\n    return 1\n";
    assert_eq!(owner(python, 2, true).as_deref(), Some("early"));
    let fields = "pub struct S {\n\n    a: u8,\n}\n";
    assert_eq!(owner(fields, 2, false).as_deref(), Some("S"));
    // The blank line after a closed one-line function and an `impl` header stays outside.
    let after = "pub fn one() {}\n\nimpl S {\n\n    fn m() {}\n}\n";
    assert_eq!(owner(after, 2, false), None);
    assert_eq!(owner(after, 4, false), None);
}
#[test]
fn a_type_and_a_function_of_one_name_are_one_symbol() {
    let source = "pub struct Same;\n\npub fn Same() {\n    let a = 1;\n}\n";
    let diff = "+++ b/a.rs\n@@ -1,1 +1,1 @@\n+pub struct Same;\n@@ -4,1 +4,1 @@\n+    let a = 1;\n";
    let symbols = changed_symbols(diff, &|_| Some(source.to_owned()));
    assert_eq!(symbols, [kind("Same")]);
}
#[test]
fn a_trailing_comment_does_not_change_whether_a_line_opens_a_block() {
    let closed = "pub fn one() {} // {\n\nfn two() {}\n";
    assert_eq!(owner(closed, 2, false), None);
    let open = "pub fn one() { // note \"{\n\n}\n";
    assert_eq!(owner(open, 2, false).as_deref(), Some("one"));
    let python = "def early(): # note\n\n    return 1\n";
    assert_eq!(owner(python, 2, true).as_deref(), Some("early"));
    let hash = "x = \"a#b\"\n\ndef later():\n    pass\n";
    assert_eq!(owner(hash, 2, true), None);
}
