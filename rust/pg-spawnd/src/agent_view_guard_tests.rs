//! Parse every production source; restrict collector constructors, not the spawner's launch path.
use syn::{visit::Visit, Expr, ExprCall, ExprLit, Item, Lit};

#[derive(Default)]
struct Guard {
    constructors: usize,
    invalid: bool,
}
impl<'ast> Visit<'ast> for Guard {
    fn visit_expr_call(&mut self, call: &'ast ExprCall) {
        if let Expr::Path(path) = &*call.func {
            let parts: Vec<_> = path
                .path
                .segments
                .iter()
                .map(|s| s.ident.to_string())
                .collect();
            if parts.last().is_some_and(|s| s == "new") {
                if parts
                    .iter()
                    .any(|s| ["HashSet", "Arc"].contains(&s.as_str()))
                {
                    return;
                }
                self.constructors += 1;
                self.invalid |= !matches!(call.args.first(), Some(Expr::Lit(ExprLit {
                    lit: Lit::Str(s), ..
                })) if s.value() == "ssh");
            }
        }
        syn::visit::visit_expr_call(self, call);
    }
    fn visit_item(&mut self, item: &'ast Item) {
        // Test fixtures can launch helpers; they are not collector execution paths.
        if let Item::Mod(module) = item {
            if module.ident == "tests" {
                return;
            }
        }
        syn::visit::visit_item(self, item);
    }
}
fn collector_guard(source: &str) -> Guard {
    let tree = syn::parse_file(source).expect("valid Rust module");
    let mut guard = Guard::default();
    // Inspect all constructors conservatively: aliases cannot bypass the check.
    guard.visit_file(&tree);
    guard
}
#[test]
fn only_literal_ssh_constructor() {
    for source in [
        "fn f() { Command::new(\"ssh\"); }",
        "fn f() { std::process::Command::new(\"ssh\"); }",
    ] {
        assert!(!collector_guard(source).invalid);
    }
    for source in [
        "fn f() { let program = \"claude\"; Command::new(program); }",
        "const PROGRAM: &str = \"opencode\"; fn f() { Command::new(PROGRAM); }",
        "use std::process::Command as Runner; fn f() { Runner::new(\"sh\"); }",
        "fn f() { std::process::Command::new(\"claude\"); }",
    ] {
        assert!(collector_guard(source).invalid);
    }
}
#[test]
fn no_runtime_invocation() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut constructors = 0;
    for entry in std::fs::read_dir(root).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_none_or(|s| s != "rs") {
            continue;
        }
        let name = path.file_name().unwrap().to_string_lossy();
        if name.ends_with("tests.rs") {
            continue;
        }
        let source = std::fs::read_to_string(&path).unwrap();
        let tree = syn::parse_file(&source).expect("production source parses");
        if name == "agent_view.rs" || name == "agent_view_projection.rs" {
            let guard = collector_guard(&source);
            assert!(!guard.invalid, "non-SSH constructor in {name}");
            constructors += guard.constructors;
        }
        // Includes CLI routing and interpolated SSH strings, across all modules.
        struct Verbs;
        impl<'a> Visit<'a> for Verbs {
            fn visit_lit_str(&mut self, literal: &'a syn::LitStr) {
                let value = literal.value();
                for runtime in ["claude", "opencode"] {
                    for verb in ["ps", "status", "run", "start", "exec"] {
                        assert!(
                            !value.contains(&format!("{runtime} {verb}")),
                            "runtime verb"
                        );
                    }
                }
            }
        }
        Verbs.visit_file(&tree);
    }
    assert_eq!(constructors, 1);
    assert!(include_str!("agent_view.rs").contains("pg-spawnd agents-view --json --local"));
    assert!(include_str!("cli.rs").contains("agent_view::execute"));
}
