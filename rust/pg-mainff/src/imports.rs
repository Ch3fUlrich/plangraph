//! Literal Python imports from committed trees, including lazy and relative imports.
use crate::process::Runner;
use rustpython_ast::{self as ast, Visitor};
use rustpython_parser::Parse;
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

pub fn git(run: &impl Runner, repo: &Path, args: &[&str]) -> Result<String, String> {
    let path = repo.to_str().ok_or("REFUSED repository path unavailable")?;
    let mut command = vec!["git", "-C", path];
    command.extend(args);
    run.run(&command)
        .ok()
        .filter(|o| o.code == 0)
        .map(|o| o.out.trim_end_matches('\n').into())
        .ok_or_else(|| format!("REFUSED git {} failed", args[0]))
}
fn relative(package: &str, level: usize, name: &str) -> String {
    if level == 0 {
        return name.into();
    }
    let parts: Vec<_> = package.split('.').collect();
    let base = parts[..parts.len().saturating_sub(level - 1)].join(".");
    if name.is_empty() {
        base
    } else {
        format!("{base}.{name}")
    }
}
struct Imports {
    package: String,
    names: Vec<String>,
}
fn literal(expr: &ast::Expr) -> Option<&str> {
    if let ast::Expr::Constant(node) = expr {
        if let ast::Constant::Str(s) = &node.value {
            return Some(s);
        }
    }
    None
}
impl Visitor for Imports {
    fn visit_stmt_import(&mut self, node: ast::StmtImport) {
        self.names
            .extend(node.names.into_iter().map(|a| a.name.to_string()));
    }
    fn visit_stmt_import_from(&mut self, node: ast::StmtImportFrom) {
        let level = node.level.map_or(0, |n| n.to_usize());
        let base = relative(
            &self.package,
            level,
            node.module.as_ref().map_or("", |s| s.as_str()),
        );
        self.names.push(base.clone());
        self.names
            .extend(node.names.into_iter().map(|a| format!("{base}.{}", a.name)));
    }
    fn visit_expr_call(&mut self, node: ast::ExprCall) {
        let recognized = match node.func.as_ref() {
            ast::Expr::Name(n) => matches!(n.id.as_str(), "__import__" | "import_module"),
            ast::Expr::Attribute(n) => n.attr.as_str() == "import_module",
            _ => false,
        };
        if recognized {
            if let Some(name) = node.args.first().and_then(literal) {
                let level = name.bytes().take_while(|c| *c == b'.').count();
                let package = node
                    .args
                    .get(1)
                    .and_then(literal)
                    .or_else(|| {
                        node.keywords
                            .iter()
                            .find(|k| k.arg.as_ref().is_some_and(|a| a.as_str() == "package"))
                            .and_then(|k| literal(&k.value))
                    })
                    .unwrap_or(&self.package);
                self.names.push(relative(package, level, &name[level..]));
            }
        }
        self.generic_visit_expr_call(node);
    }
}
fn paths(run: &impl Runner, repo: &Path, revision: &str) -> Result<BTreeSet<String>, String> {
    Ok(
        git(run, repo, &["ls-tree", "-r", "-z", "--name-only", revision])?
            .split('\0')
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect(),
    )
}
fn closure(
    run: &impl Runner,
    repo: &Path,
    revision: &str,
    roots: Option<&BTreeSet<String>>,
) -> Result<BTreeSet<String>, String> {
    let mut modules = BTreeMap::new();
    for path in paths(run, repo, revision)? {
        if let Some(stem) = path.strip_suffix(".py") {
            let name = stem.strip_prefix("src/").unwrap_or(stem).replace('/', ".");
            modules.insert(
                name.strip_suffix(".__init__").unwrap_or(&name).to_owned(),
                path,
            );
        }
    }
    let mut pending: Vec<_> = modules
        .iter()
        .filter(|(n, p)| {
            (n.as_str() == "plangraph.keeper" || n.starts_with("plangraph.keeper."))
                && roots.is_none_or(|r| r.contains(*p))
        })
        .map(|(n, _)| n.clone())
        .collect();
    let mut seen = BTreeSet::new();
    while let Some(name) = pending.pop() {
        let Some(path) = modules.get(&name) else {
            continue;
        };
        if !seen.insert(name.clone()) {
            continue;
        }
        let source = git(run, repo, &["show", &format!("{revision}:{path}")])?;
        let package = if path.ends_with("/__init__.py") {
            name.as_str()
        } else {
            name.rsplit_once('.').map_or("", |(p, _)| p)
        };
        let mut visitor = Imports {
            package: package.into(),
            names: vec![name.clone()],
        };
        for stmt in
            ast::Suite::parse(&source, path).map_err(|_| "keeper import analysis unavailable")?
        {
            visitor.visit_stmt(stmt);
        }
        for imported in visitor.names {
            let parts: Vec<_> = imported.split('.').collect();
            for i in 1..=parts.len() {
                let prefix = parts[..i].join(".");
                if modules.contains_key(&prefix) && !seen.contains(&prefix) {
                    pending.push(prefix);
                }
            }
        }
    }
    Ok(seen
        .into_iter()
        .filter_map(|name| modules.remove(&name))
        .collect())
}
pub fn changes(
    run: &impl Runner,
    repo: &Path,
    loaded: &str,
    target: &str,
) -> Result<Vec<String>, String> {
    let old = paths(run, repo, loaded)?;
    let mut watched = closure(run, repo, loaded, None)?;
    watched.extend(closure(run, repo, target, Some(&old))?);
    // Runtime reads outside the Python import graph.
    watched.extend(
        [
            "src/plangraph/routing_support/catalog_schema.sql",
            "src/plangraph/planner/route_policy.json",
            "configuration/opencode/providers.json",
        ]
        .into_iter()
        .map(str::to_owned),
    );
    let diff = git(
        run,
        repo,
        &["diff", "--no-renames", "-z", "--name-only", loaded, target],
    )?;
    Ok(diff
        .split('\0')
        .filter(|p| {
            old.contains(*p) && (p.starts_with("src/plangraph/keeper/") || watched.contains(*p))
        })
        .map(str::to_owned)
        .collect())
}
