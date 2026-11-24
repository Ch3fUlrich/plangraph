//! Locate libtest functions and overlay only test code onto the base source.
use super::super::Result;
use quote::ToTokens;
use std::{collections::BTreeMap, fs, ops::Range, path::Path};
use syn::{spanned::Spanned, Attribute, Item, ItemMod};

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(super) struct Name(pub Vec<String>);

impl Name {
    pub(super) fn qualified(&self, module: &[String]) -> String {
        module
            .iter()
            .chain(&self.0)
            .cloned()
            .collect::<Vec<_>>()
            .join("::")
    }
}

fn parse(src: &str) -> Result<syn::File> {
    syn::parse_file(src).map_err(|error| error.to_string())
}

fn walk<'a>(
    items: &'a [Item],
    prefix: &mut Vec<String>,
    tests: &mut BTreeMap<Name, &'a Item>,
) -> Result<()> {
    for item in items {
        match item {
            Item::Fn(func)
                if func.attrs.iter().any(|a| {
                    a.path()
                        .segments
                        .last()
                        .is_some_and(|segment| segment.ident == "test")
                }) =>
            {
                prefix.push(func.sig.ident.to_string());
                if tests.insert(Name(prefix.clone()), item).is_some() {
                    return Err(format!(
                        "ambiguous cfg-dependent test {}",
                        prefix.join("::")
                    ));
                }
                prefix.pop();
            }
            Item::Mod(module) => {
                if let Some((_, items)) = &module.content {
                    prefix.push(module.ident.to_string());
                    walk(items, prefix, tests)?;
                    prefix.pop();
                }
            }
            _ => {}
        }
    }
    Ok(())
}

fn tests(file: &syn::File) -> Result<BTreeMap<Name, &Item>> {
    let mut result = BTreeMap::new();
    walk(&file.items, &mut Vec::new(), &mut result)?;
    Ok(result)
}

pub(super) fn changed(base: &str, head: &str) -> Result<Vec<Name>> {
    let base = parse(base)?;
    let head = parse(head)?;
    let before = tests(&base)?;
    Ok(tests(&head)?
        .into_iter()
        .filter_map(|(name, item)| {
            let old = before
                .get(&name)
                .map(|item| item.to_token_stream().to_string());
            (old.as_deref() != Some(&item.to_token_stream().to_string())).then_some(name)
        })
        .collect())
}

fn module<'a>(items: &'a [Item], name: &str) -> Option<&'a ItemMod> {
    items.iter().find_map(|item| match item {
        Item::Mod(module) if module.ident == name => Some(module),
        _ => None,
    })
}

fn test_only(attrs: &[Attribute]) -> bool {
    attrs.iter().any(|attr| {
        attr.path().is_ident("cfg")
            && attr
                .parse_args::<syn::Ident>()
                .is_ok_and(|ident| ident == "test")
    })
}

fn item_range(item: &Item) -> Range<usize> {
    item.span().byte_range()
}

struct Insertion<'a> {
    items: &'a [Item],
    at: usize,
}

fn edit_range(
    base: Insertion<'_>,
    head: &[Item],
    modules: &[String],
    test: &Item,
    prior: Option<&Item>,
) -> Option<(Range<usize>, Range<usize>)> {
    let empty = || base.at..base.at;
    let Some((first, rest)) = modules.split_first() else {
        return Some((prior.map_or_else(empty, item_range), item_range(test)));
    };
    let head = module(head, first)?;
    let (_, head_items) = head.content.as_ref()?;
    match module(base.items, first) {
        Some(base) if test_only(&base.attrs) && test_only(&head.attrs) => {
            Some((base.span().byte_range(), head.span().byte_range()))
        }
        Some(base) => {
            let (brace, items) = base.content.as_ref()?;
            edit_range(
                Insertion {
                    items,
                    at: brace.span.close().byte_range().start,
                },
                head_items,
                rest,
                test,
                prior,
            )
        }
        None if test_only(&head.attrs) => Some((empty(), head.span().byte_range())),
        None => None,
    }
}

/// Preserve every production byte. A missing scope is insertable only when it is test-only.
pub(super) fn overlay(base: &str, head: &str, names: &[Name]) -> Result<(String, Vec<Name>)> {
    let base_file = parse(base)?;
    let head_file = parse(head)?;
    let head_tests = tests(&head_file)?;
    let base_tests = tests(&base_file)?;
    let mut edits = Vec::<(Range<usize>, Range<usize>)>::new();
    let mut missing = Vec::new();
    for name in names {
        let test = head_tests
            .get(name)
            .ok_or("changed Rust test disappeared")?;
        let edit = edit_range(
            Insertion {
                items: &base_file.items,
                at: base.len(),
            },
            &head_file.items,
            &name.0[..name.0.len() - 1],
            test,
            base_tests.get(name).copied(),
        );
        match edit {
            Some(edit) if !edits.contains(&edit) => edits.push(edit),
            Some(_) => {}
            None => missing.push(name.clone()),
        }
    }
    edits.sort_by_key(|(base, head)| (base.start, head.start));
    let mut result = base.to_owned();
    for (to, from) in edits.into_iter().rev() {
        let text = head.get(from).ok_or("invalid Rust test span")?;
        if result.get(to.clone()).is_none() {
            return Err("invalid Rust overlay span".into());
        }
        result.replace_range(to, &format!("\n{text}\n"));
    }
    Ok((result, missing))
}

fn explicit_path(attrs: &[Attribute]) -> Option<std::path::PathBuf> {
    attrs.iter().find_map(|attr| {
        if let syn::Meta::NameValue(value) = &attr.meta {
            if value.path.is_ident("path") {
                if let syn::Expr::Lit(syn::ExprLit {
                    lit: syn::Lit::Str(path),
                    ..
                }) = &value.value
                {
                    return Some(path.value().into());
                }
            }
        }
        None
    })
}

/// Visit out-of-line modules with their libtest namespace from a Cargo target root.
pub(super) fn module_files(root: &Path, target: &Path, wanted: &Path) -> Result<Vec<Vec<String>>> {
    let mut matches = Vec::new();
    let Some(dir) = target.parent() else {
        return Ok(matches);
    };
    visit_file(
        root,
        target,
        wanted,
        dir,
        dir,
        &mut Vec::new(),
        &mut Vec::new(),
        &mut matches,
    )?;
    Ok(matches)
}

#[allow(clippy::too_many_arguments)]
fn visit_file(
    root: &Path,
    path: &Path,
    wanted: &Path,
    dir: &Path,
    parent: &Path,
    namespace: &mut Vec<String>,
    stack: &mut Vec<std::path::PathBuf>,
    matches: &mut Vec<Vec<String>>,
) -> Result<()> {
    let Ok(path) = path.canonicalize() else {
        return Ok(());
    };
    if !path.starts_with(root) || stack.contains(&path) {
        return Ok(());
    }
    if path == wanted {
        matches.push(namespace.clone());
        return Ok(());
    }
    stack.push(path.clone());
    let source = fs::read_to_string(path).map_err(|err| err.to_string())?;
    visit_modules(
        root,
        wanted,
        &parse(&source)?.items,
        dir,
        parent,
        namespace,
        stack,
        matches,
    )?;
    stack.pop();
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn visit_modules(
    root: &Path,
    wanted: &Path,
    items: &[Item],
    dir: &Path,
    parent: &Path,
    namespace: &mut Vec<String>,
    stack: &mut Vec<std::path::PathBuf>,
    matches: &mut Vec<Vec<String>>,
) -> Result<()> {
    for item in items {
        let Item::Mod(module) = item else { continue };
        let name = module.ident.to_string();
        namespace.push(name.clone());
        let child_dir = dir.join(&name);
        if let Some((_, items)) = &module.content {
            visit_modules(
                root, wanted, items, &child_dir, &child_dir, namespace, stack, matches,
            )?;
        } else {
            let override_path = explicit_path(&module.attrs).map(|path| parent.join(path));
            let flat = dir.join(format!("{name}.rs"));
            let nested = child_dir.join("mod.rs");
            let (path, submodules) = match override_path {
                Some(path) => {
                    let directory = path.parent().unwrap_or(dir).to_owned();
                    (path, directory)
                }
                None if flat.is_file() => (flat, child_dir),
                None => (nested, child_dir),
            };
            visit_file(
                root,
                &path,
                wanted,
                &submodules,
                path.parent().unwrap_or(dir),
                namespace,
                stack,
                matches,
            )?;
        }
        namespace.pop();
    }
    Ok(())
}

#[cfg(test)]
mod tests_replay {
    use super::*;

    #[test]
    fn rust_overlay_copies_test_fixtures_and_preserves_production() {
        let base = "fn answer() -> u8 { 1 }\n#[cfg(test)] mod tests { fn helper() {} }";
        let head = concat!(
            "fn answer() -> u8 { 2 }\n",
            "#[cfg(test)] mod tests { fn expected() -> u8 { 2 } ",
            "#[test] fn behavior() { assert_eq!(super::answer(), expected()); } }",
        );
        let names = changed(base, head).unwrap();
        assert_eq!(
            names
                .iter()
                .map(|name| name.qualified(&[]))
                .collect::<Vec<_>>(),
            ["tests::behavior"]
        );
        let (actual, missing) = overlay(base, head, &names).unwrap();
        assert!(missing.is_empty());
        assert!(actual.starts_with("fn answer() -> u8 { 1 }\n"));
        assert!(actual.contains("fn expected() -> u8 { 2 }"));
        assert!(actual.contains("fn behavior()"));
    }

    #[test]
    fn rust_overlay_refuses_to_add_a_production_scope_missing_on_base() {
        let head = "mod feature { #[test] fn behavior() { panic!(); } }";
        let names = changed("", head).unwrap();
        let (actual, missing) = overlay("", head, &names).unwrap();
        assert_eq!(missing, names);
        assert!(actual.is_empty());
    }
}
