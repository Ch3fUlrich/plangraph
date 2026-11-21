use super::{git, markdown, semantic, Index, Item, NavResult, RecentCommit, Section, Symbol};
use serde::Serialize;
use std::collections::BTreeSet;
use std::path::Path;

#[derive(Serialize)]
pub struct Navigation {
    pub index_commit: String,
    pub worktree_head: String,
    pub stale: bool,
    pub changed_files: Vec<String>,
    pub symbols: Vec<Symbol>,
    pub sections: Vec<Section>,
    pub rules: Vec<Section>,
    pub items: Vec<Item>,
    pub definitions: Vec<semantic::Definition>,
    pub references: Vec<semantic::Reference>,
    pub callers: Vec<Call>,
    pub callees: Vec<Call>,
    pub implementations: Vec<semantic::Definition>,
    pub last_commits: Vec<RecentCommit>,
    pub scip_covered_paths: Vec<String>,
}

#[derive(Serialize)]
pub struct Call {
    pub site: super::Location,
    pub caller: Option<super::Location>,
    pub target: semantic::ScipSymbol,
    pub definitions: Vec<super::Location>,
}

pub(super) fn lookup(mut index: Index, root: &Path, key: &str) -> NavResult<Navigation> {
    let worktree_head = git::head(root)?;
    let changed_files = git::changed(root, &index.index_commit, &worktree_head)?;
    let stale = index.index_commit != worktree_head || !changed_files.is_empty();

    let mut requested = BTreeSet::from([key]);
    let item_refs = index
        .items
        .iter()
        .find(|item| item.key == key)
        .map(|item| item.refs.clone())
        .unwrap_or_default();
    requested.extend(item_refs.iter().map(String::as_str));
    let mut selected_rules = BTreeSet::new();
    for (id, section) in index.sections.iter().enumerate() {
        if markdown::is_rule(&section.location.path)
            && (requested.contains(section.location.path.as_str())
                || requested.contains(section.title.as_str())
                || markdown::tokens(&section.title).any(|word| requested.contains(word)))
        {
            selected_rules.insert(id);
        }
    }
    let mut named = requested.clone();
    for &id in &selected_rules {
        named.extend(index.sections[id].terms.iter().map(String::as_str));
    }

    let mut selected = BTreeSet::new();
    let mut aliases = BTreeSet::new();
    for (id, symbol) in index.symbols.iter().enumerate() {
        let reference = format!("{}::{}", symbol.location.path, symbol.qualified_name);
        if [
            symbol.name.as_str(),
            symbol.qualified_name.as_str(),
            symbol.location.path.as_str(),
            reference.as_str(),
        ]
        .iter()
        .any(|term| named.contains(term))
        {
            selected.insert(id);
            aliases.insert(symbol.name.clone());
            aliases.insert(symbol.qualified_name.clone());
            aliases.insert(reference);
        }
    }

    let mut paths: BTreeSet<_> = requested
        .iter()
        .filter(|path| index.tracked_paths.contains(**path))
        .map(|path| (*path).to_owned())
        .collect();
    for &id in &selected {
        paths.insert(index.symbols[id].location.path.clone());
    }
    let mut doc_ids = BTreeSet::new();
    for (id, section) in index.sections.iter().enumerate() {
        if selected_rules.contains(&id)
            || requested.contains(section.title.as_str())
            || requested.contains(section.location.path.as_str())
            || section.terms.iter().any(|term| {
                requested.contains(term.as_str()) || aliases.contains(term) || paths.contains(term)
            })
        {
            doc_ids.insert(id);
        }
    }
    for &id in &doc_ids {
        if !markdown::is_rule(&index.sections[id].location.path) {
            paths.insert(index.sections[id].location.path.clone());
        }
    }
    for &term in &requested {
        if index.scip_covered_files.contains(term) {
            paths.insert(term.to_owned());
        }
    }

    let semantic_ids: BTreeSet<_> = index
        .semantic
        .definitions
        .iter()
        .filter(|definition| {
            definition
                .syntax_symbol
                .is_some_and(|id| selected.contains(&id))
                || requested.contains(definition.location.path.as_str())
                || requested.contains(definition.symbol.symbol.as_str())
        })
        .map(|definition| definition.symbol.clone())
        .collect();
    let mut reference_ids = semantic_ids.clone();
    let mut implementation_ids = BTreeSet::new();
    let mut definition_ids = semantic_ids.clone();
    for relation in &index.semantic.relationships {
        if semantic_ids.contains(&relation.target) {
            if relation.is_reference {
                reference_ids.insert(relation.symbol.clone());
            }
            if relation.is_implementation {
                implementation_ids.insert(relation.symbol.clone());
            }
        }
        if semantic_ids.contains(&relation.symbol) && relation.is_definition {
            definition_ids.insert(relation.target.clone());
        }
    }
    let mut definitions_by_id = std::collections::BTreeMap::<_, Vec<_>>::new();
    for definition in &index.semantic.definitions {
        definitions_by_id
            .entry(&definition.symbol)
            .or_default()
            .push(definition.location.clone());
    }
    let mut callers = Vec::new();
    let mut callees = Vec::new();
    let call = |reference: &semantic::Reference| Call {
        site: reference.location.clone(),
        caller: reference
            .caller
            .map(|id| index.symbols[id].location.clone()),
        target: reference.target.clone(),
        definitions: definitions_by_id
            .get(&reference.target)
            .cloned()
            .unwrap_or_default(),
    };
    let mut references = Vec::new();
    for reference in std::mem::take(&mut index.semantic.references) {
        if reference.is_call && reference.caller.is_some_and(|id| selected.contains(&id)) {
            callees.push(call(&reference));
        }
        if reference_ids.contains(&reference.target) {
            if reference.is_call {
                callers.push(call(&reference));
            }
            references.push(reference);
        }
    }
    let (mut definitions, mut implementations) = (Vec::new(), Vec::new());
    for definition in index.semantic.definitions {
        if definition_ids.contains(&definition.symbol) {
            definitions.push(definition);
        } else if implementation_ids.contains(&definition.symbol) {
            implementations.push(definition);
        }
    }
    let items = index
        .items
        .into_iter()
        .filter(|item| {
            item.key == key
                || item.refs.iter().any(|term| {
                    requested.contains(term.as_str())
                        || aliases.contains(term)
                        || paths.contains(term)
                })
        })
        .collect();
    let last_commits = git::history(
        root,
        &index.index_commit,
        &paths.iter().map(String::as_str).collect::<Vec<_>>(),
    )?;
    let (rules, sections) = index
        .sections
        .into_iter()
        .enumerate()
        .filter(|(id, _)| doc_ids.contains(id))
        .map(|(_, section)| section)
        .partition(|section| markdown::is_rule(&section.location.path));
    Ok(Navigation {
        index_commit: index.index_commit,
        worktree_head,
        stale,
        changed_files,
        symbols: index
            .symbols
            .into_iter()
            .enumerate()
            .filter(|(id, _)| selected.contains(id))
            .map(|(_, symbol)| symbol)
            .collect(),
        sections,
        rules,
        items,
        definitions,
        references,
        callers,
        callees,
        implementations,
        last_commits,
        scip_covered_paths: index
            .scip_covered_files
            .into_iter()
            .filter(|path| paths.contains(path))
            .collect(),
    })
}
