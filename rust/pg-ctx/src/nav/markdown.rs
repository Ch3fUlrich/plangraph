use super::{syntax, NavResult, Section, Symbol};
use tree_sitter::Node;

pub(super) fn tokens(text: &str) -> impl Iterator<Item = &str> {
    text.split(|c: char| !c.is_alphanumeric() && !matches!(c, '_' | '-' | '.' | '/' | ':'))
        .map(|word| word.trim_matches(['.', ':', '/']))
        .filter(|word| !word.is_empty())
}

pub(super) fn extract(path: &str, source: &str) -> NavResult<(Vec<Symbol>, Vec<Section>)> {
    let tree = syntax::parse(&tree_sitter_md::LANGUAGE.into(), source)?;
    let mut symbols = Vec::new();
    let mut sections = Vec::new();
    let root = tree.root_node();
    let mut cursor = root.walk();
    for node in root
        .named_children(&mut cursor)
        .filter(|node| node.kind() == "section")
    {
        visit(
            path,
            source,
            node,
            &mut Vec::new(),
            &mut symbols,
            &mut sections,
        );
    }
    Ok((symbols, sections))
}

fn visit<'a>(
    path: &str,
    source: &'a str,
    node: Node<'_>,
    scope: &mut Vec<&'a str>,
    symbols: &mut Vec<Symbol>,
    sections: &mut Vec<Section>,
) {
    let heading = node
        .named_child(0)
        .filter(|child| matches!(child.kind(), "atx_heading" | "setext_heading"));
    let title = heading
        .and_then(|node| node.child_by_field_name("heading_content"))
        .map(|content| syntax::text(content, source).trim());
    let mut cursor = node.walk();
    let children: Vec<_> = node
        .named_children(&mut cursor)
        .filter(|child| child.kind() == "section")
        .collect();
    if let Some(title) = title {
        scope.push(title);
        symbols.push(Symbol {
            name: title.to_owned(),
            qualified_name: scope.join("::"),
            kind: "heading".into(),
            location: syntax::location(path, node),
            signature: syntax::text(heading.unwrap(), source).trim().to_owned(),
            doc_comment: None,
        });
    }
    let end = children
        .first()
        .map_or(node.end_byte(), |next| next.start_byte());
    let content = &source[node.start_byte()..end];
    if !content.trim().is_empty() {
        let mut location = syntax::location(path, node);
        location.end_line = children
            .first()
            .map_or(location.end_line, |next| next.start_position().row);
        sections.push(Section {
            title: title.unwrap_or(path).to_owned(),
            location,
            terms: section_terms(content),
        });
    }
    if is_rule(path) {
        for table in node
            .named_children(&mut cursor)
            .filter(|child| child.kind() == "pipe_table")
        {
            decision_rows(path, source, table, sections);
        }
    }
    for child in children {
        visit(path, source, child, scope, symbols, sections);
    }
    if title.is_some() {
        scope.pop();
    }
}

fn section_terms(content: &str) -> Vec<String> {
    let mut terms: Vec<_> = tokens(content).map(str::to_owned).collect();
    terms.sort_unstable();
    terms.dedup();
    terms
}

fn decision_rows(path: &str, source: &str, table: Node<'_>, sections: &mut Vec<Section>) {
    let mut cursor = table.walk();
    for row in table
        .named_children(&mut cursor)
        .filter(|node| node.kind() == "pipe_table_row")
    {
        let Some(first) = row.named_child(0) else {
            continue;
        };
        let title = syntax::text(first, source).trim().trim_matches('`');
        if is_decision_id(title) {
            sections.push(Section {
                title: title.to_owned(),
                location: syntax::location(path, row),
                terms: section_terms(syntax::text(row, source)),
            });
        }
    }
}

pub(super) fn is_rule(path: &str) -> bool {
    path == "router/DECISIONS.md"
}

pub(super) fn is_decision_id(word: &str) -> bool {
    word.strip_prefix("D-")
        .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
}

pub(super) fn is_rule_id(word: &str) -> bool {
    is_decision_id(word)
        || (word.contains('.')
            && word
                .split('.')
                .all(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit())))
}

pub(super) fn is_doc(path: &str) -> bool {
    path == "AGENTS.md" || path.starts_with("docs/") || path.starts_with("skills/") || is_rule(path)
}
