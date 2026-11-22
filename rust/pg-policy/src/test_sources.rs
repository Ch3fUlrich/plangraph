//! Portable-test guard: inspect Rust expressions rather than matching stringified fixtures.
use crate::deadline_cases;
use regex::Regex;
use std::{collections::HashMap, fs, path::Path};
use syn::{visit::Visit, Expr, Lit, Pat};

fn literal(expr: &Expr) -> Option<String> {
    match expr {
        Expr::Lit(value) => match &value.lit {
            Lit::Str(s) => Some(s.value()),
            Lit::Char(c) => Some(c.value().to_string()),
            _ => None,
        },
        _ => None,
    }
}

#[derive(Default)]
struct Reads {
    // true means an unnormalized file read; aliases retain that provenance.
    bindings: HashMap<String, bool>,
    lines: Vec<usize>,
}
impl Reads {
    fn raw(&self, expr: &Expr) -> bool {
        match expr {
            Expr::Call(call) => {
                matches!(&*call.func, Expr::Path(p) if p.path.segments.last().is_some_and(|s| s.ident == "read_to_string"))
                    || call.args.iter().any(|arg| self.raw(arg))
            }
            Expr::MethodCall(call) => {
                let args: Vec<_> = call.args.iter().filter_map(literal).collect();
                let normalized = call.method == "replace"
                    && args.len() == 2
                    && ((args[0] == "\r\n" && args[1] == "\n")
                        || (args[0] == "\r" && args[1].is_empty()));
                !normalized && self.raw(&call.receiver)
            }
            Expr::Path(path) => path.path.get_ident().is_some_and(|name| {
                self.bindings
                    .get(&name.to_string())
                    .copied()
                    .unwrap_or(false)
            }),
            Expr::Try(e) => self.raw(&e.expr),
            Expr::Paren(e) => self.raw(&e.expr),
            Expr::Reference(e) => self.raw(&e.expr),
            Expr::Field(e) => self.raw(&e.base),
            _ => false,
        }
    }
}
impl<'ast> Visit<'ast> for Reads {
    fn visit_block(&mut self, node: &'ast syn::Block) {
        let prior = self.bindings.clone();
        let locals: Vec<_> = node
            .stmts
            .iter()
            .filter_map(|stmt| {
                let syn::Stmt::Local(local) = stmt else {
                    return None;
                };
                let pat = match &local.pat {
                    Pat::Type(p) => &*p.pat,
                    p => p,
                };
                if let Pat::Ident(name) = pat {
                    Some(name.ident.to_string())
                } else {
                    None
                }
            })
            .collect();
        syn::visit::visit_block(self, node);
        // Shadowed locals expire; assignments to outer bindings survive.
        for name in locals {
            if let Some(raw) = prior.get(&name) {
                self.bindings.insert(name, *raw);
            } else {
                self.bindings.remove(&name);
            }
        }
    }
    fn visit_item_fn(&mut self, node: &'ast syn::ItemFn) {
        // Each helper/test has its own bindings, including same-named locals.
        let prior = std::mem::take(&mut self.bindings);
        syn::visit::visit_item_fn(self, node);
        self.bindings = prior;
    }
    fn visit_local(&mut self, node: &'ast syn::Local) {
        syn::visit::visit_local(self, node);
        let pat = match &node.pat {
            Pat::Type(p) => &*p.pat,
            p => p,
        };
        if let (Pat::Ident(name), Some(init)) = (pat, &node.init) {
            self.bindings
                .insert(name.ident.to_string(), self.raw(&init.expr));
        }
    }
    fn visit_expr_assign(&mut self, node: &'ast syn::ExprAssign) {
        syn::visit::visit_expr_assign(self, node);
        if let Expr::Path(path) = &*node.left {
            if let Some(name) = path.path.get_ident() {
                self.bindings
                    .insert(name.to_string(), self.raw(&node.right));
            }
        }
    }
    fn visit_expr_method_call(&mut self, node: &'ast syn::ExprMethodCall) {
        if [
            "split",
            "split_once",
            "rsplit",
            "rsplit_once",
            "splitn",
            "rsplitn",
            "split_terminator",
            "rsplit_terminator",
        ]
        .iter()
        .any(|name| node.method == name)
            && node
                .args
                .iter()
                .filter_map(literal)
                .any(|s| s.contains('\n') && !s.contains('\r'))
            && self.raw(&node.receiver)
        {
            self.lines.push(node.method.span().start().line);
        }
        syn::visit::visit_expr_method_call(self, node);
    }
}

fn scan(
    path: &Path,
    relative: &Path,
    runner: Option<&deadline_cases::Runner>,
    errors: &mut Vec<String>,
) -> Result<(), String> {
    let source = fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let name = relative.to_string_lossy().replace('\\', "/");
    let in_tests = relative
        .components()
        .any(|p| p.as_os_str() == "tests" || p.as_os_str() == "test")
        || relative.file_stem().is_some_and(|s| {
            let s = s.to_string_lossy();
            s.starts_with("test_")
                || s.ends_with("_tests")
                || s.ends_with("_test")
                || s.ends_with(".test")
                || s.ends_with(".spec")
        });
    let rust = path.extension().is_some_and(|s| s == "rs");
    if !in_tests && !(rust && (source.contains("#[test]") || source.contains("::test]"))) {
        return Ok(());
    }
    let homes = Regex::new(r"/home/[A-Za-z0-9_.-]+").unwrap();
    for (line, text) in source.lines().enumerate() {
        if homes.is_match(text) {
            errors.push(format!(
                "{name}:{}: host home literal; use a temporary or synthetic path",
                line + 1
            ));
        }
    }
    if rust {
        let tree = syn::parse_file(&source).map_err(|e| format!("{name}: {e}"))?;
        deadline_cases::scan(&tree, relative, &name, runner, errors);
        let mut reads = Reads::default();
        reads.visit_file(&tree);
        errors.extend(reads.lines.into_iter().map(|line| {
            format!("{name}:{line}: CRLF normalization required before splitting a file read on LF")
        }));
    }
    Ok(())
}

pub(crate) fn check(root: &Path) -> Result<Vec<String>, String> {
    fn walk(
        root: &Path,
        dir: &Path,
        runner: Option<&deadline_cases::Runner>,
        errors: &mut Vec<String>,
    ) -> Result<(), String> {
        for entry in fs::read_dir(dir).map_err(|e| format!("{}: {e}", dir.display()))? {
            let entry = entry.map_err(|e| e.to_string())?;
            let path = entry.path();
            let kind = entry.file_type().map_err(|e| e.to_string())?;
            if kind.is_dir() {
                if ![
                    ".git",
                    "target",
                    "vendor",
                    "node_modules",
                    ".venv",
                    "__pycache__",
                ]
                .iter()
                .any(|s| entry.file_name() == *s)
                {
                    walk(root, &path, runner, errors)?;
                }
            } else if kind.is_file()
                && path.extension().is_some_and(|s| {
                    ["rs", "py", "js", "ts", "jsx", "tsx", "sh"]
                        .iter()
                        .any(|ext| s == *ext)
                })
            {
                scan(&path, path.strip_prefix(root).unwrap(), runner, errors)?;
            }
        }
        Ok(())
    }
    let mut errors = Vec::new();
    let runner = deadline_cases::runner(root)?;
    walk(root, root, runner.as_ref(), &mut errors)?;
    errors.sort();
    Ok(errors)
}
