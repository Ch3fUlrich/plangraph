//! V1 durable-fact contract and transport-independent memory operations.
#![cfg_attr(
    not(feature = "mock"),
    doc = r#"
The default library does not expose an in-memory fallback:
```compile_fail,E0432
use og_memory::io::MockTransport;
```
"#
)]
#![cfg_attr(
    feature = "mock",
    doc = r#"
Offline callers must explicitly enable the `mock` feature:
```
use og_memory::io::MockTransport;
let _ = MockTransport::default();
```
"#
)]
pub mod io;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Envelope {
    pub version: u32,
    pub project: String,
    pub kind: String,
    pub slug: String,
    pub content: String,
    pub source: Source,
    pub actor: String,
    pub relations: Vec<Relation>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub r#type: String,
    pub path: String,
    pub hash: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Relation {
    pub edge: String,
    pub target_slug: String,
}

/// Payload-free errors: dynamic input is never retained, even in Debug.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Refusal {
    UnsupportedVersion,
    UnknownKind,
    Task,
    RoutingSlug,
    InvalidProject,
    InvalidSlug,
    MissingSourceHash,
    MissingEdge,
    Secret,
    MissingType(&'static str),
    MissingProperty(&'static str, &'static str),
}
impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for Refusal {}

#[derive(Default, Debug)]
pub struct Capabilities {
    nodes: BTreeMap<String, BTreeSet<String>>,
    edges: BTreeMap<String, (String, String)>,
}
impl Capabilities {
    /// Reads node/property and edge declarations, ignoring // comments and annotations.
    /// Unrecognized syntax grants no capabilities (this is not a full .pg compiler).
    pub fn from_schema(schema: &str) -> Self {
        let mut caps = Self::default();
        let mut node = None;
        let text = schema
            .lines()
            .map(|l| l.split("//").next().unwrap_or(""))
            .collect::<Vec<_>>()
            .join("\n")
            .replace('{', "{\n")
            .replace('}', "\n}\n");
        for line in text.lines().map(str::trim) {
            let words: Vec<_> = line.split_whitespace().collect();
            match words.as_slice() {
                ["node", name, "{"] => {
                    caps.nodes.entry((*name).into()).or_default();
                    node = Some((*name).to_owned());
                }
                ["}"] => node = None,
                ["edge", name, from, "->", to] if name.ends_with(':') => {
                    caps.edges.insert(
                        name.trim_end_matches(':').into(),
                        ((*from).into(), (*to).into()),
                    );
                }
                _ => {
                    if let (Some(n), Some((property, _))) = (&node, line.split_once(':')) {
                        let property = property.trim();
                        if !property.starts_with('@') {
                            caps.nodes.get_mut(n).unwrap().insert(property.into());
                        }
                    }
                }
            }
        }
        caps
    }
}

// V1's sole property-name table: (target type, hub edge, property <- value).
// Value selectors: slug, content, accepted, empty, must, project.
type Mapping = (
    &'static str,
    &'static str,
    &'static [(&'static str, &'static str)],
);
const V1: &[Mapping] = &[
    (
        "Decision",
        "DecidedIn",
        &[
            ("slug", "slug"),
            ("title", "slug"),
            ("rationale", "content"),
            ("status", "accepted"),
            ("date", "empty"),
        ],
    ),
    (
        "Rule",
        "ConstrainsProject",
        &[
            ("slug", "slug"),
            ("statement", "content"),
            ("severity", "must"),
        ],
    ),
    (
        "Convention",
        "AppliesTo",
        &[("slug", "slug"), ("name", "slug"), ("example", "content")],
    ),
    (
        "Preference",
        "",
        &[
            ("slug", "slug"),
            ("statement", "content"),
            ("scope", "project"),
        ],
    ),
    ("Project", "", &[("slug", "project")]),
];

#[derive(Debug)]
pub struct Validated {
    envelope: Envelope,
    properties: BTreeMap<&'static str, String>,
    hub_edge: &'static str,
}
impl Validated {
    pub fn envelope(&self) -> &Envelope {
        &self.envelope
    }
    pub fn properties(&self) -> &BTreeMap<&'static str, String> {
        &self.properties
    }
    pub fn hub_edge(&self) -> &'static str {
        self.hub_edge
    }
}
fn slug_ok(s: &str) -> bool {
    !s.is_empty()
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        && !s.starts_with('-')
        && !s.ends_with('-')
        && !s.contains("--")
}
pub fn validate(e: &Envelope, caps: &Capabilities) -> Result<Validated, Refusal> {
    if [&e.project, &e.kind, &e.slug, &e.content]
        .into_iter()
        .chain([&e.source.r#type, &e.source.path, &e.source.hash, &e.actor])
        .chain(e.relations.iter().flat_map(|r| [&r.edge, &r.target_slug]))
        .any(|s| pg_worker_core::redact::redact(s) != *s)
    {
        return Err(Refusal::Secret);
    }
    if e.version != 1 {
        return Err(Refusal::UnsupportedVersion);
    }
    if e.kind == "Task" {
        return Err(Refusal::Task);
    }
    let mapping = V1[..4]
        .iter()
        .find(|m| m.0 == e.kind)
        .ok_or(Refusal::UnknownKind)?;
    if [&e.project, &e.slug]
        .into_iter()
        .chain(e.relations.iter().map(|r| &r.target_slug))
        .any(|s| s.starts_with("routing-"))
    {
        return Err(Refusal::RoutingSlug);
    }
    if !slug_ok(&e.project) {
        return Err(Refusal::InvalidProject);
    }
    if !slug_ok(&e.slug) || e.relations.iter().any(|r| !slug_ok(&r.target_slug)) {
        return Err(Refusal::InvalidSlug);
    }
    if e.source.hash.trim().is_empty() {
        return Err(Refusal::MissingSourceHash);
    }
    for m in [mapping, &V1[4]] {
        let props = caps.nodes.get(m.0).ok_or(Refusal::MissingType(m.0))?;
        for (p, _) in m.2 {
            if !props.contains(*p) {
                return Err(Refusal::MissingProperty(m.0, p));
            }
        }
    }
    let edge_ok = |edge: &str| {
        caps.edges
            .get(edge)
            .is_some_and(|(from, to)| from == &e.kind && to == "Project")
    };
    if (!mapping.1.is_empty() && !edge_ok(mapping.1))
        || e.relations.iter().any(|r| {
            !edge_ok(&r.edge) || (mapping.1.is_empty() && V1.iter().any(|m| m.1 == r.edge))
        })
    {
        return Err(Refusal::MissingEdge);
    }
    let properties = mapping
        .2
        .iter()
        .map(|(p, selector)| {
            (
                *p,
                match *selector {
                    "slug" => e.slug.clone(),
                    "content" => e.content.clone(),
                    "project" => e.project.clone(),
                    "empty" => String::new(),
                    literal => literal.into(),
                },
            )
        })
        .collect();
    Ok(Validated {
        envelope: e.clone(),
        properties,
        hub_edge: mapping.1,
    })
}

#[cfg(test)]
mod tests;
