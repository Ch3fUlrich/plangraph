//! Inspect points that can invoke Cargo during a Rust test deadline.
use std::{collections::BTreeSet, fs, path::Path};
use syn::{
    punctuated::Punctuated, spanned::Spanned, visit::Visit, Expr, Item, ItemFn, Lit, LitStr, Meta,
    Pat, Token,
};

const RUNNER: &str = "rust/worker-core/src/bin/pg-test-deadline.rs";
const OVERRIDE: &str = "deadline-build: ";

#[derive(Default)]
pub(crate) struct Runner {
    cases: BTreeSet<(String, String)>,
    preparation: Option<String>,
}

fn literal(expr: &Expr) -> Option<String> {
    match expr {
        Expr::Lit(lit) => string(&lit.lit),
        _ => None,
    }
}

fn string(lit: &Lit) -> Option<String> {
    match lit {
        Lit::Str(value) => Some(value.value()),
        _ => None,
    }
}

struct CasePatterns(BTreeSet<(String, String)>);

impl<'ast> Visit<'ast> for CasePatterns {
    fn visit_pat_tuple(&mut self, pattern: &'ast syn::PatTuple) {
        if pattern.elems.len() == 2 {
            if let (Pat::Lit(suite), Pat::Lit(case)) = (&pattern.elems[0], &pattern.elems[1]) {
                if let (Some(suite), Some(case)) = (string(&suite.lit), string(&case.lit)) {
                    self.0.insert((suite, case));
                }
            }
        }
        syn::visit::visit_pat_tuple(self, pattern);
    }
}

pub(crate) fn runner(root: &Path) -> Result<Option<Runner>, String> {
    let src = match fs::read_to_string(root.join(RUNNER)) {
        Ok(src) => src,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("{RUNNER}: {error}")),
    };
    let mut config = Runner::default();
    let syntax = syn::parse_file(&src).map_err(|e| format!("{RUNNER}: {e}"))?;
    for item in &syntax.items {
        match item {
            Item::Fn(function) if function.sig.ident == "build_deadline" => {
                let mut cases = CasePatterns(BTreeSet::new());
                cases.visit_item_fn(function);
                config.cases = cases.0;
            }
            Item::Const(item) if item.ident == "CLI_FIXTURE_TEST" => {
                config.preparation = literal(&item.expr);
            }
            _ => {}
        }
    }
    Ok(Some(config))
}

fn marker(function: &ItemFn) -> Option<String> {
    function.attrs.iter().find_map(|attr| match &attr.meta {
        Meta::NameValue(meta) if meta.path.is_ident("doc") => {
            Some(literal(&meta.value)?.strip_prefix(OVERRIDE)?.to_owned())
        }
        _ => None,
    })
}

fn is_test(function: &ItemFn) -> bool {
    function.attrs.iter().any(|attr| {
        attr.path()
            .segments
            .last()
            .is_some_and(|p| p.ident == "test")
    })
}

#[derive(Default)]
struct TestNames(BTreeSet<String>);

impl<'ast> Visit<'ast> for TestNames {
    fn visit_item_fn(&mut self, function: &'ast ItemFn) {
        if is_test(function) {
            self.0.insert(function.sig.ident.to_string());
        }
        syn::visit::visit_item_fn(self, function);
    }
}

struct Scan<'a> {
    file: &'a str,
    suite: &'a str,
    tests: BTreeSet<String>,
    runner: Option<&'a Runner>,
    function: String,
    covered: bool,
    errors: &'a mut Vec<String>,
}

impl Scan<'_> {
    fn covers(&self, function: &ItemFn) -> bool {
        let Some(marker) = marker(function) else {
            return false;
        };
        if let Some(case) = marker.strip_prefix("case=") {
            return is_test(function)
                && function.sig.ident == case
                && self
                    .runner
                    .is_none_or(|r| r.cases.contains(&(self.suite.to_owned(), case.to_owned())));
        }
        marker.strip_prefix("fixture=").is_some_and(|case| {
            self.tests.contains(case)
                && !is_test(function)
                && self.runner.is_none_or(|r| {
                    r.preparation
                        .as_deref()
                        .is_some_and(|name| name.trim_start_matches("::") == case)
                })
        })
    }

    fn report(&mut self, span: proc_macro2::Span) {
        if !self.covered {
            self.errors.push(format!(
                "{}:{}: {} starts nested Cargo without a deadline build override or prepared fixture",
                self.file,
                span.start().line,
                self.function,
            ));
        }
    }
}

impl<'ast> Visit<'ast> for Scan<'_> {
    fn visit_item_fn(&mut self, function: &'ast ItemFn) {
        let previous = std::mem::replace(&mut self.function, function.sig.ident.to_string());
        let covered = if marker(function).is_some() || is_test(function) {
            self.covers(function)
        } else {
            self.covered
        };
        let previous_coverage = std::mem::replace(&mut self.covered, covered);
        syn::visit::visit_item_fn(self, function);
        self.function = previous;
        self.covered = previous_coverage;
    }

    fn visit_expr_call(&mut self, call: &'ast syn::ExprCall) {
        if let Expr::Path(callee) = &*call.func {
            let mut path = callee.path.segments.iter().rev();
            if let (Some(method), Some(owner)) = (path.next(), path.next()) {
                let direct_cargo = owner.ident == "Command"
                    && method.ident == "new"
                    && matches!(
                        call.args.first().and_then(literal).as_deref(),
                        Some("cargo" | "cargo.exe")
                    );
                let shared_builder = owner.ident == "binary" && method.ident == "build";
                if direct_cargo || shared_builder {
                    self.report(call.span());
                }
            }
        }
        syn::visit::visit_expr_call(self, call);
    }

    fn visit_macro(&mut self, call: &'ast syn::Macro) {
        if call.path.is_ident("env") || call.path.is_ident("option_env") {
            let args = call.parse_body_with(Punctuated::<LitStr, Token![,]>::parse_terminated);
            if let Some(name) = args.ok().and_then(|args| args.first().map(LitStr::value)) {
                // Cargo resolves CARGO_BIN_EXE_* to a prebuilt binary before tests start.
                if name.starts_with("CARGO_BIN_EXE_") {
                    return;
                }
                if name == "CARGO" {
                    self.report(call.span());
                }
            }
        }
        syn::visit::visit_macro(self, call);
    }
}

pub(crate) fn scan(
    tree: &syn::File,
    relative: &Path,
    file: &str,
    runner: Option<&Runner>,
    errors: &mut Vec<String>,
) {
    let mut names = TestNames::default();
    names.visit_file(tree);
    Scan {
        file,
        suite: relative.file_stem().and_then(|s| s.to_str()).unwrap_or(""),
        tests: names.0,
        runner,
        function: "<module>".to_owned(),
        covered: false,
        errors,
    }
    .visit_file(tree);
}
