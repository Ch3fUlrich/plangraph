use super::{syntax::Syntax, Location, NavResult, ScipInput};
use protobuf::Message;
use scip::types::{occurrence::Typed_range, Document, Occurrence, SymbolRole};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::ops::Range;

#[derive(Clone, Copy)]
enum PositionEncoding {
    Utf8,
    Utf16,
    Utf32,
}

impl PositionEncoding {
    fn from_scip(value: i32, tool: &str) -> Option<Self> {
        // SCIP stores the encoding in the document. Older producers leave it unset.
        match (value, tool) {
            (1, _) | (0, "rust-analyzer") => Some(Self::Utf8),
            (2, _) | (0, "scip-python") => Some(Self::Utf16),
            (3, _) => Some(Self::Utf32),
            _ => None,
        }
    }

    fn units(self, ch: char) -> usize {
        match self {
            Self::Utf8 => ch.len_utf8(),
            Self::Utf16 => ch.len_utf16(),
            Self::Utf32 => 1,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct ScipSymbol {
    pub symbol: String,
    /// SCIP local symbols are scoped to their document.
    pub local_path: Option<String>,
}

impl ScipSymbol {
    fn new(path: &str, symbol: &str) -> Self {
        Self {
            symbol: symbol.to_owned(),
            local_path: symbol.starts_with("local ").then(|| path.to_owned()),
        }
    }
}

#[derive(Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct Reference {
    pub location: Location,
    pub start_byte_column: usize,
    pub end_byte_column: usize,
    pub target: ScipSymbol,
    pub caller: Option<usize>,
    pub is_call: bool,
}

#[derive(Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct Definition {
    pub symbol: ScipSymbol,
    pub location: Location,
    pub syntax_symbol: Option<usize>,
}

#[derive(Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct Relationship {
    pub symbol: ScipSymbol,
    pub target: ScipSymbol,
    pub is_reference: bool,
    pub is_implementation: bool,
    pub is_definition: bool,
}

#[derive(Debug, Default, Deserialize, Serialize)]
pub(super) struct Edges {
    pub definitions: BTreeSet<Definition>,
    pub references: BTreeSet<Reference>,
    pub relationships: BTreeSet<Relationship>,
}

#[derive(Default)]
pub(super) struct Inputs {
    docs: Vec<(String, Document, String)>,
}

fn relative(path: &str) -> bool {
    path.split('/')
        .all(|p| !p.is_empty() && p != "." && p != "..")
        && !path.contains(['\\', '\0'])
}

impl Inputs {
    pub(super) fn read(inputs: &[ScipInput]) -> NavResult<Self> {
        let mut result = Self::default();
        for input in inputs {
            if !input.prefix.is_empty() && !relative(&input.prefix) {
                return Err(format!("invalid SCIP repository prefix {:?}", input.prefix).into());
            }
            let index = scip::types::Index::parse_from_bytes(&fs::read(&input.file)?)
                .map_err(|error| format!("{}: {error}", input.file.display()))?;
            let tool = index.metadata.tool_info.name.clone();
            for doc in index.documents {
                if !relative(&doc.relative_path) {
                    return Err(
                        format!("invalid SCIP document path {:?}", doc.relative_path).into(),
                    );
                }
                let path = if input.prefix.is_empty() {
                    doc.relative_path.clone()
                } else {
                    format!("{}/{}", input.prefix, doc.relative_path)
                };
                result.docs.push((path, doc, tool.clone()));
            }
        }
        Ok(result)
    }

    pub(super) fn paths(&self) -> BTreeSet<String> {
        self.docs.iter().map(|(path, _, _)| path.clone()).collect()
    }

    pub(super) fn resolve(self, sources: &BTreeMap<String, SourceMap>) -> NavResult<Edges> {
        let mut edges = Edges::default();
        for (path, doc, tool) in self.docs {
            let Some(source) = sources.get(&path) else {
                // Indexers may include vendored or generated files outside the commit.
                continue;
            };
            if !doc.text.is_empty() && doc.text != source.text {
                return Err(format!("SCIP text differs from committed source for {path}").into());
            }
            let encoding = PositionEncoding::from_scip(doc.position_encoding.value(), &tool)
                .ok_or_else(|| {
                    format!("SCIP position encoding is missing or unsupported for {path}")
                })?;
            for occurrence in doc.occurrences {
                if occurrence.symbol.is_empty() {
                    continue;
                }
                source.add_occurrence(&path, encoding, occurrence, &mut edges)?;
            }
            for symbol in doc.symbols {
                for relation in symbol.relationships {
                    if relation.is_implementation || relation.is_reference || relation.is_definition
                    {
                        edges.relationships.insert(Relationship {
                            symbol: ScipSymbol::new(&path, &symbol.symbol),
                            target: ScipSymbol::new(&path, &relation.symbol),
                            is_implementation: relation.is_implementation,
                            is_reference: relation.is_reference,
                            is_definition: relation.is_definition,
                        });
                    }
                }
            }
        }
        Ok(edges)
    }
}

pub(super) struct SourceMap {
    text: String,
    line_offsets: Vec<usize>,
    names: BTreeMap<usize, (usize, usize)>,
    bodies: Vec<(Range<usize>, usize)>,
    calls: BTreeMap<usize, usize>,
}

impl SourceMap {
    pub(super) fn new(text: &str, syntax: Option<&Syntax>, first_symbol: usize) -> Self {
        let mut source = Self {
            text: text.to_owned(),
            line_offsets: std::iter::once(0)
                .chain(text.match_indices('\n').map(|(index, _)| index + 1))
                .collect(),
            names: BTreeMap::new(),
            bodies: Vec::new(),
            calls: BTreeMap::new(),
        };
        if let Some(syntax) = syntax {
            for (i, definition) in syntax.definitions.iter().enumerate() {
                let index = first_symbol + i;
                source
                    .names
                    .insert(definition.name.start, (definition.name.end, index));
                if let Some(body) = &definition.body {
                    source.bodies.push((body.clone(), index));
                }
            }
            source
                .calls
                .extend(syntax.calls.iter().map(|range| (range.start, range.end)));
        }
        source
    }

    fn position(
        &self,
        line: i32,
        column: i32,
        encoding: PositionEncoding,
    ) -> Option<(usize, usize)> {
        let line = usize::try_from(line).ok()?;
        let column = usize::try_from(column).ok()?;
        let start = *self.line_offsets.get(line)?;
        let end = self
            .line_offsets
            .get(line + 1)
            .copied()
            .unwrap_or(self.text.len());
        let text = self.text[start..end].trim_end_matches(['\r', '\n']);
        let mut units = 0;
        let mut bytes = 0;
        for ch in text.chars() {
            if units >= column {
                break;
            }
            units += encoding.units(ch);
            bytes += ch.len_utf8();
        }
        (units == column).then_some((start + bytes, bytes))
    }

    fn add_occurrence(
        &self,
        path: &str,
        encoding: PositionEncoding,
        occurrence: Occurrence,
        edges: &mut Edges,
    ) -> NavResult<()> {
        let (line, col, end_line, end_col) = match occurrence.typed_range {
            Some(Typed_range::SingleLineRange(r)) => {
                (r.line, r.start_character, r.line, r.end_character)
            }
            Some(Typed_range::MultiLineRange(r)) => {
                (r.start_line, r.start_character, r.end_line, r.end_character)
            }
            None => match *occurrence.range.as_slice() {
                [line, col, end] => (line, col, line, end),
                [line, col, end_line, end] => (line, col, end_line, end),
                _ => return Err(format!("invalid SCIP range for {path}").into()),
            },
            Some(_) => return Err(format!("unsupported SCIP range for {path}").into()),
        };
        let (Some((start, start_byte_column)), Some((end, end_byte_column))) = (
            self.position(line, col, encoding),
            self.position(end_line, end_col, encoding),
        ) else {
            return Err(format!(
                "SCIP range outside committed source for {path}:{}",
                line + 1
            )
            .into());
        };
        if end < start {
            return Err(format!("reversed SCIP range for {path}:{}", line + 1).into());
        }
        let location = Location {
            path: path.to_owned(),
            start_line: line as usize + 1,
            end_line: (end_line as usize + usize::from(end_col != 0)).max(line as usize + 1),
        };
        let target = ScipSymbol::new(path, &occurrence.symbol);
        if occurrence.symbol_roles & SymbolRole::Definition as i32 != 0 {
            edges.definitions.insert(Definition {
                location,
                symbol: target,
                syntax_symbol: self
                    .names
                    .get(&start)
                    .filter(|(last, _)| *last == end)
                    .map(|(_, index)| *index),
            });
        } else {
            let is_call = self.calls.get(&start) == Some(&end);
            let caller = is_call
                .then(|| {
                    self.bodies
                        .iter()
                        .filter(|(body, _)| body.start <= start && end <= body.end)
                        .min_by_key(|(body, _)| body.len())
                        .map(|(_, index)| *index)
                })
                .flatten();
            edges.references.insert(Reference {
                location,
                start_byte_column,
                end_byte_column,
                target,
                caller,
                is_call,
            });
        }
        Ok(())
    }
}
