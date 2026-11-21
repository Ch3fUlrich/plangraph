use super::{Location, NavResult, Symbol};
use std::ops::Range;
use tree_sitter::{Language, Node, Parser};

pub(super) fn language(path: &str) -> Option<Language> {
    Some(match path.rsplit_once('.')?.1 {
        "rs" => tree_sitter_rust::LANGUAGE.into(),
        "py" | "pyi" => tree_sitter_python::LANGUAGE.into(),
        "js" | "jsx" | "cjs" | "mjs" => tree_sitter_javascript::LANGUAGE.into(),
        "ts" | "mts" | "cts" => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
        "tsx" => tree_sitter_typescript::LANGUAGE_TSX.into(),
        "sh" | "bash" => tree_sitter_bash::LANGUAGE.into(),
        "md" => tree_sitter_md::LANGUAGE.into(),
        _ => return None,
    })
}

pub(super) fn shell_script(source: &str) -> Option<Language> {
    let shebang = source.lines().next()?.strip_prefix("#!")?;
    shebang
        .split_whitespace()
        .filter_map(|word| word.rsplit('/').next())
        .any(|name| matches!(name, "sh" | "bash"))
        .then(|| tree_sitter_bash::LANGUAGE.into())
}

pub(super) fn text<'a>(node: Node<'_>, source: &'a str) -> &'a str {
    &source[node.byte_range()]
}

pub(super) fn location(path: &str, node: Node<'_>) -> Location {
    let end = node.end_position();
    Location {
        path: path.to_owned(),
        start_line: node.start_position().row + 1,
        end_line: (end.row + usize::from(end.column != 0)).max(node.start_position().row + 1),
    }
}

pub(super) fn parse(language: &Language, source: &str) -> NavResult<tree_sitter::Tree> {
    let mut parser = Parser::new();
    parser.set_language(language)?;
    parser
        .parse(source, None)
        .ok_or_else(|| "tree-sitter parse cancelled".into())
}

pub(super) struct Definition {
    pub symbol: Symbol,
    pub name: Range<usize>,
    pub body: Option<Range<usize>>,
}

pub(super) struct Syntax {
    pub definitions: Vec<Definition>,
    pub calls: Vec<Range<usize>>,
}

fn symbol_kind(node: Node<'_>) -> Option<&'static str> {
    Some(match node.kind() {
        "function_item"
        | "function_signature_item"
        | "function_definition"
        | "function_declaration"
        | "generator_function_declaration" => "function",
        "method_definition" | "method_signature" | "abstract_method_signature" => "method",
        "class_definition" | "class_declaration" | "abstract_class_declaration" => "class",
        "struct_item" => "struct",
        "enum_item" | "enum_declaration" => "enum",
        "enum_variant" => "variant",
        "trait_item" => "trait",
        "impl_item" => "implementation",
        "mod_item" | "internal_module" => "module",
        "type_item" | "type_alias_declaration" => "type",
        "union_item" => "union",
        "interface_declaration" => "interface",
        "const_item" => "constant",
        "static_item" => "static",
        "macro_definition" => "macro",
        "variable_declarator"
            if node.child_by_field_name("value").is_some_and(|value| {
                matches!(value.kind(), "arrow_function" | "function_expression")
            }) =>
        {
            "function"
        }
        _ => return None,
    })
}

fn doc_comment(node: Node<'_>, source: &str) -> Option<String> {
    if let Some(first) = node
        .child_by_field_name("body")
        .and_then(|body| body.named_child(0))
    {
        if let Some(string) = first
            .named_child(0)
            .filter(|child| child.kind() == "string")
        {
            if first.kind() == "expression_statement" {
                return Some(
                    text(string, source)
                        .trim_matches(['\'', '"'])
                        .trim()
                        .to_owned(),
                );
            }
        }
    }
    let declaration = node
        .parent()
        .filter(|p| p.kind() == "decorated_definition")
        .unwrap_or(node);
    let mut sibling = declaration.prev_named_sibling();
    let mut next_line = declaration.start_position().row;
    let mut comments = Vec::new();
    while let Some(previous) = sibling {
        if previous.end_position().row + 1 < next_line {
            break;
        }
        next_line = previous.start_position().row;
        sibling = previous.prev_named_sibling();
        if previous.kind() == "attribute_item" {
            continue;
        }
        let value = text(previous, source).trim();
        if let Some(doc) = value
            .strip_prefix("///")
            .filter(|doc| !doc.starts_with('/'))
        {
            comments.push(doc.trim());
        } else if let Some(doc) = value
            .strip_prefix("/**")
            .and_then(|doc| doc.strip_suffix("*/"))
        {
            comments.push(doc.trim());
        } else if node.kind() == "function_definition" && value.starts_with('#') {
            comments.push(value.trim_start_matches('#').trim());
        } else {
            break;
        }
    }
    comments.reverse();
    (!comments.is_empty()).then(|| comments.join("\n"))
}

fn call_target(node: Node<'_>) -> Option<Node<'_>> {
    let target = match node.kind() {
        "call" | "call_expression" => node.child_by_field_name("function"),
        "new_expression" => node.child_by_field_name("constructor"),
        "command" => node.child_by_field_name("name"),
        "macro_invocation" => node.child_by_field_name("macro"),
        _ => None,
    }?;
    let mut target = target;
    loop {
        let field = match target.kind() {
            "attribute" => "attribute",
            "field_expression" => "field",
            "member_expression" => "property",
            "scoped_identifier" => "name",
            "generic_function" => "function",
            _ => return Some(target),
        };
        target = target.child_by_field_name(field)?;
    }
}

pub(super) fn extract(path: &str, source: &str, tree: &tree_sitter::Tree) -> Syntax {
    let mut syntax = Syntax {
        definitions: Vec::new(),
        calls: Vec::new(),
    };
    visit(path, source, tree.root_node(), &mut Vec::new(), &mut syntax);
    syntax
}

fn visit<'a>(
    path: &str,
    source: &'a str,
    node: Node<'_>,
    scope: &mut Vec<&'a str>,
    syntax: &mut Syntax,
) {
    let mut scoped = false;
    if let Some(kind) = symbol_kind(node) {
        let name = node.child_by_field_name("name").or_else(|| {
            (node.kind() == "impl_item")
                .then(|| node.child_by_field_name("type"))
                .flatten()
        });
        if let Some(name) = name {
            let name_text = text(name, source);
            scope.push(name_text);
            scoped = true;
            let value = node.child_by_field_name("value");
            let body = node
                .child_by_field_name("body")
                .or_else(|| value.and_then(|value| value.child_by_field_name("body")));
            let signature_end = body
                .or(value)
                .filter(|_| node.kind() != "variable_declarator")
                .map_or_else(
                    || body.map_or(node.end_byte(), |body| body.start_byte()),
                    |n| n.start_byte(),
                );
            syntax.definitions.push(Definition {
                symbol: Symbol {
                    name: name_text.to_owned(),
                    qualified_name: scope.join("::"),
                    kind: kind.into(),
                    location: location(path, node),
                    signature: source[node.start_byte()..signature_end].trim().to_owned(),
                    doc_comment: doc_comment(node, source),
                },
                name: name.byte_range(),
                body: matches!(kind, "function" | "method")
                    .then(|| body.map(|body| body.byte_range()))
                    .flatten(),
            });
        }
    }
    if let Some(target) = call_target(node) {
        syntax.calls.push(target.byte_range());
    }
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        visit(path, source, child, scope, syntax);
    }
    if scoped {
        scope.pop();
    }
}
