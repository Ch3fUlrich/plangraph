//! Schema-owned policy defaults, not admission caps or runtime wiring.
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuleSpec {
    pub name: String,
    pub unit: String,
    pub kind: String,
    pub default: u64,
    pub min: u64,
    pub max: u64,
    pub alarm: Option<u64>,
    pub overrides: Vec<String>,
    pub host_seeds: BTreeMap<String, u64>,
}

#[derive(Debug, thiserror::Error)]
pub enum RuleError {
    #[error("rules_schema")]
    Schema,
    #[error("rules_io")]
    Io,
    #[error("rules_override")]
    InvalidOverride,
}

#[derive(Debug, Default)]
pub struct RuleLayers {
    pub process: BTreeMap<String, String>,
    pub env_file: BTreeMap<String, String>,
    pub keeper_host: BTreeMap<String, String>,
    pub keeper_fleet: BTreeMap<String, String>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct RuleReport {
    pub values: BTreeMap<String, u64>,
    pub sources: BTreeMap<String, String>,
    pub diagnostics: Vec<String>,
}

fn unsigned(s: &str) -> Option<u64> {
    (!s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
        .then(|| s.parse().ok())
        .flatten()
}

fn quoted(s: &str) -> Option<String> {
    let body = s.strip_prefix('"')?.strip_suffix('"')?;
    (!body.contains(['"', '\\']) && !body.chars().any(char::is_control)).then(|| body.to_owned())
}

// Deliberately restricted single-line TOML subset, not a general TOML parser.
fn atom(s: &str) -> Option<Value> {
    if let Some(body) = s.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
        let items = if body.trim().is_empty() {
            Vec::new()
        } else {
            body.split(',')
                .map(|v| quoted(v.trim()).map(Value::String))
                .collect::<Option<_>>()?
        };
        return Some(Value::Array(items));
    }
    if let Some(body) = s.strip_prefix('{').and_then(|s| s.strip_suffix('}')) {
        let mut table = Map::new();
        for pair in body.split(',').filter(|_| !body.trim().is_empty()) {
            let (key, value) = pair.split_once('=')?;
            let key = key.trim();
            let key = quoted(key).or_else(|| valid_key(key).then(|| key.to_owned()))?;
            if table.insert(key, unsigned(value.trim())?.into()).is_some() {
                return None;
            }
        }
        return Some(Value::Object(table));
    }
    quoted(s)
        .map(Value::String)
        .or_else(|| unsigned(s).map(Value::from))
}

fn valid_key(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

fn parse_schema(text: &str) -> Result<Vec<RuleSpec>, RuleError> {
    let mut version = false;
    let mut rows = Vec::<Map<String, Value>>::new();
    for line in text
        .lines()
        .map(str::trim)
        .filter(|s| !s.is_empty() && !s.starts_with('#'))
    {
        if line == "[[rules]]" && version {
            rows.push(Map::new());
            continue;
        }
        let (key, value) = line.split_once('=').ok_or(RuleError::Schema)?;
        let (key, value) = (key.trim(), value.trim());
        if rows.is_empty() {
            if key != "version" || value != "1" || version {
                return Err(RuleError::Schema);
            }
            version = true;
        } else if rows
            .last_mut()
            .unwrap()
            .insert(key.into(), atom(value).ok_or(RuleError::Schema)?)
            .is_some()
        {
            return Err(RuleError::Schema);
        }
    }
    if !version || rows.is_empty() {
        return Err(RuleError::Schema);
    }
    let specs: Vec<RuleSpec> = rows
        .into_iter()
        .map(|r| serde_json::from_value(Value::Object(r)).map_err(|_| RuleError::Schema))
        .collect::<Result<_, _>>()?;
    let mut keys = BTreeSet::new();
    for s in &specs {
        let in_range = |v| (s.min..=s.max).contains(&v);
        if s.kind != "u64"
            || !valid_key(&s.name)
            || !valid_key(&s.unit)
            || !keys.insert(env_key(s))
            || !in_range(s.default)
            || s.alarm.is_some_and(|v| !in_range(v))
            || s.host_seeds
                .iter()
                .any(|(k, v)| !["dispatch_vm", "prox"].contains(&k.as_str()) || !in_range(*v))
            || s.overrides.iter().any(|v| {
                !["process", "env_file", "keeper_host", "keeper_fleet"].contains(&v.as_str())
            })
            || s.overrides.iter().collect::<BTreeSet<_>>().len() != s.overrides.len()
        {
            return Err(RuleError::Schema);
        }
    }
    Ok(specs)
}

pub fn schema() -> Result<Vec<RuleSpec>, RuleError> {
    parse_schema(include_str!("../../../setup/placement-rules.schema.toml"))
}

fn env_key(s: &RuleSpec) -> String {
    let mut name = String::new();
    for (i, c) in s.name.chars().enumerate() {
        if i > 0 && c.is_ascii_uppercase() {
            name.push('_');
        }
        name.push(c.to_ascii_uppercase());
    }
    format!("PG_RULE_{name}_{}", s.unit)
}

/// Host class must come from a trusted registry adapter, not a caller identity.
pub fn resolve(specs: &[RuleSpec], host_class: &str, layers: &RuleLayers) -> RuleReport {
    let mut report = RuleReport::default();
    let levels = [
        ("process", &layers.process),
        ("env_file", &layers.env_file),
        ("keeper_host", &layers.keeper_host),
        ("keeper_fleet", &layers.keeper_fleet),
    ];
    let known: BTreeSet<_> = specs.iter().map(env_key).collect();
    for (source, layer) in levels {
        for _key in layer
            .keys()
            .filter(|k| k.starts_with("PG_RULE_") && !known.contains(*k))
        {
            // Do not echo arbitrary untrusted key text into diagnostics.
            report
                .diagnostics
                .push(format!("PG_RULE_<unknown> {source} unknown_key"));
        }
    }
    for spec in specs {
        let key = env_key(spec);
        let mut selected = (
            *spec.host_seeds.get(host_class).unwrap_or(&spec.default),
            "default",
        );
        for (source, layer) in levels {
            if let Some(raw) = layer.get(&key) {
                let value = unsigned(raw);
                let reason = if !spec.overrides.iter().any(|v| v == source) {
                    "disallowed_source"
                } else if value.is_none() {
                    "invalid_unsigned"
                } else if !(spec.min..=spec.max).contains(&value.unwrap()) {
                    "out_of_range"
                } else {
                    selected = (value.unwrap(), source);
                    break;
                };
                report.diagnostics.push(format!("{key} {source} {reason}"));
            }
        }
        report.values.insert(key.clone(), selected.0);
        report.sources.insert(key, selected.1.into());
    }
    report
}

/// No shell syntax or interpolation. On error discard the entire file layer.
pub fn parse_env_file(text: &str) -> Result<BTreeMap<String, String>, RuleError> {
    let mut out = BTreeMap::new();
    for line in text
        .lines()
        .map(str::trim)
        .filter(|s| !s.is_empty() && !s.starts_with('#'))
    {
        let (key, value) = line.split_once('=').ok_or(RuleError::InvalidOverride)?;
        let (key, value) = (key.trim(), value.trim());
        if !valid_key(key)
            || value.contains(['$', '`'])
            || out.insert(key.into(), value.into()).is_some()
        {
            return Err(RuleError::InvalidOverride);
        }
    }
    Ok(out)
}

#[cfg(test)]
#[path = "placement_rules_tests.rs"]
mod tests;
