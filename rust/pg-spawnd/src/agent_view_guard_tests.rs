//! Check process command construction, not unrelated value constructors.
use std::collections::HashMap;
use syn::{visit::Visit, Expr, ExprCall, ExprLit, Item, Lit, UseTree};

#[derive(Default)]
struct Guard {
    aliases: HashMap<String, String>,
    commands: std::collections::HashSet<String>,
    invalid: bool,
}
fn imports(tree: &UseTree, prefix: &str, aliases: &mut HashMap<String, String>) {
    match tree {
        UseTree::Path(p) => imports(&p.tree, &format!("{prefix}{}::", p.ident), aliases),
        UseTree::Name(n) => {
            aliases.insert(n.ident.to_string(), format!("{prefix}{}", n.ident));
        }
        UseTree::Rename(r) => {
            aliases.insert(r.rename.to_string(), format!("{prefix}{}", r.ident));
        }
        UseTree::Group(g) => {
            for item in &g.items {
                imports(item, prefix, aliases);
            }
        }
        UseTree::Glob(_) => {}
    }
}
impl Guard {
    fn command(&self, call: &ExprCall) -> bool {
        let Expr::Path(path) = &*call.func else {
            return false;
        };
        let mut parts: Vec<_> = path
            .path
            .segments
            .iter()
            .map(|s| s.ident.to_string())
            .collect();
        if parts.last().is_none_or(|s| s != "new") {
            return false;
        }
        parts.pop();
        let mut name = parts.join("::");
        for _ in 0..self.aliases.len() + 1 {
            let (first, rest) = name.split_once("::").unwrap_or((&name, ""));
            let Some(alias) = self.aliases.get(first) else {
                break;
            };
            let next = if rest.is_empty() {
                alias.clone()
            } else {
                format!("{alias}::{rest}")
            };
            if next == name {
                break;
            }
            name = next;
        }
        matches!(
            name.as_str(),
            "std::process::Command" | "tokio::process::Command"
        )
    }
}
impl<'ast> Visit<'ast> for Guard {
    fn visit_local(&mut self, local: &'ast syn::Local) {
        if let (syn::Pat::Ident(binding), Some(init)) = (&local.pat, &local.init) {
            if let Expr::Call(call) = &*init.expr {
                if self.command(call) {
                    self.commands.insert(binding.ident.to_string());
                }
            }
        }
        syn::visit::visit_local(self, local);
    }
    fn visit_expr_call(&mut self, call: &'ast ExprCall) {
        if self.command(call) {
            self.invalid |= !matches!(call.args.first(), Some(Expr::Lit(ExprLit {
                lit: Lit::Str(s), ..
            })) if s.value() == "ssh");
        }
        syn::visit::visit_expr_call(self, call);
    }
    fn visit_expr_method_call(&mut self, call: &'ast syn::ExprMethodCall) {
        fn command_receiver(guard: &Guard, expr: &Expr) -> bool {
            match expr {
                Expr::Call(call) => guard.command(call),
                Expr::Path(path) => path
                    .path
                    .get_ident()
                    .is_some_and(|name| guard.commands.contains(&name.to_string())),
                Expr::MethodCall(call) => command_receiver(guard, &call.receiver),
                _ => false,
            }
        }
        if ["arg", "args"].iter().any(|name| call.method == *name)
            && command_receiver(self, &call.receiver)
        {
            struct Words(bool);
            impl<'a> Visit<'a> for Words {
                fn visit_lit_str(&mut self, literal: &'a syn::LitStr) {
                    self.0 |= literal
                        .value()
                        .split(|c: char| !c.is_alphanumeric() && c != '_')
                        .any(|word| ["claude", "opencode"].contains(&word));
                }
            }
            let mut words = Words(false);
            for arg in &call.args {
                words.visit_expr(arg);
            }
            self.invalid |= words.0;
        }
        syn::visit::visit_expr_method_call(self, call);
    }
    fn visit_item(&mut self, item: &'ast Item) {
        if matches!(item, Item::Mod(module) if module.ident == "tests") {
            return;
        }
        syn::visit::visit_item(self, item);
    }
}
fn collector_guard(source: &str) -> Guard {
    let tree = syn::parse_file(source).expect("valid Rust module");
    struct Imports(HashMap<String, String>);
    impl<'a> Visit<'a> for Imports {
        fn visit_item_use(&mut self, item: &'a syn::ItemUse) {
            imports(&item.tree, "", &mut self.0);
        }
        fn visit_item_type(&mut self, item: &'a syn::ItemType) {
            if let syn::Type::Path(path) = &*item.ty {
                self.0.insert(
                    item.ident.to_string(),
                    path.path
                        .segments
                        .iter()
                        .map(|s| s.ident.to_string())
                        .collect::<Vec<_>>()
                        .join("::"),
                );
            }
        }
    }
    let mut names = Imports(HashMap::new());
    names.visit_file(&tree);
    let mut guard = Guard {
        aliases: names.0,
        commands: Default::default(),
        invalid: false,
    };
    guard.visit_file(&tree);
    guard
}
#[test]
fn process_commands_only() {
    for source in [
        r#"use std::process::Command; fn f() { Command::new("ssh").args(["host", "inventory"]); String::new(); Mutex::new(0); }"#,
        r#"use tokio::process as proc; type Runner = proc::Command; fn f() { Runner::new("ssh"); }"#,
    ] {
        assert!(!collector_guard(source).invalid);
    }
    for source in [
        r#"use std::process::Command; fn f() { Command::new(program); }"#,
        r#"use std::process::Command as Runner; fn f() { Runner::new("sh"); }"#,
        r#"use tokio::process as proc; fn f() { proc::Command::new("ssh").args(["claude", "ps"]); }"#,
        r#"fn f() { std::process::Command::new("ssh").arg("opencode status"); }"#,
        r#"use std::process::{Command as Runner}; type Launch = Runner; fn f() { Launch::new("other"); }"#,
        r#"use std::process::Command; fn f() { let mut child = Command::new("ssh"); child.args(["claude", "ps"]); }"#,
    ] {
        assert!(collector_guard(source).invalid);
    }
}
#[test]
fn no_runtime_invocation() {
    for source in [
        include_str!("agent_view.rs"),
        include_str!("agent_view_projection.rs"),
    ] {
        assert!(!collector_guard(source).invalid, "unsafe collector command");
    }
    assert!(include_str!("agent_view.rs").contains("pg-spawnd agents-view --json --local"));
    assert!(include_str!("cli.rs").contains("agent_view::execute"));
}
