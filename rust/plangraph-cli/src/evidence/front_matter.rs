use saphyr::{LoadableYamlNode, Scalar, Yaml};
use serde_json::{Map, Value};

const ARTICLE_FIELDS: &[&str] = &[
    "id",
    "title",
    "date",
    "status",
    "components",
    "stage",
    "level",
    "decision_ids",
    "item_keys",
    "supersedes",
    "superseded_by",
    "last_reviewed",
    "evidence_class",
];
const RESULT_FIELDS: &[&str] = &[
    "metric",
    "unit",
    "direction",
    "design",
    "baseline",
    "after",
    "effect",
    "certainty",
    "outcome",
    "gain_class",
    "claim_id",
];

pub(super) struct Article<'a> {
    metadata: Map<String, Value>,
    pub(super) results: Vec<Map<String, Value>>,
    pub(super) body: &'a str,
}

impl Article<'_> {
    pub(super) fn claim_ids(&self) -> impl Iterator<Item = &str> {
        self.results
            .iter()
            .map(|result| result["claim_id"].as_str().expect("validated claim_id"))
    }

    pub(super) fn index_rows(&self, path: &str) -> impl Iterator<Item = Map<String, Value>> + '_ {
        let mut metadata = self.metadata.clone();
        metadata.insert("path".into(), Value::from(path));
        self.results
            .iter()
            .enumerate()
            .map(move |(number, result)| {
                let mut row = metadata.clone();
                row.insert("result".into(), Value::from(number + 1));
                row.extend(result.clone());
                row
            })
    }
}

pub(super) fn parse(text: &str) -> Result<Article<'_>, String> {
    let mut lines = text.split_inclusive('\n');
    let opening = lines
        .next()
        .filter(|line| line.trim_end() == "---")
        .ok_or("missing opening YAML front matter delimiter")?;
    let mut offset = opening.len();
    let (header, body) = loop {
        let line = lines
            .next()
            .ok_or("missing closing YAML front matter delimiter")?;
        if line.trim_end() == "---" {
            break (&text[opening.len()..offset], &text[offset + line.len()..]);
        }
        offset += line.len();
    };
    let documents = Yaml::load_from_str(header)
        .map_err(|error| format!("invalid YAML front matter: {error}"))?;
    let [Yaml::Mapping(metadata)] = documents.as_slice() else {
        return Err("front matter must contain one YAML mapping".into());
    };
    let article = project(metadata, ARTICLE_FIELDS)?;
    for field in ["title", "date", "status"] {
        nonempty_string(&article, field)?;
    }
    required(&article, "superseded_by")?;

    let Some(Yaml::Sequence(results)) = metadata.get(&yaml_key("results")) else {
        return Err("front matter requires a results sequence".into());
    };
    if results.is_empty() {
        return Err("front matter results must contain at least one result".into());
    }
    let results = results
        .iter()
        .enumerate()
        .map(|(number, result)| {
            parse_result(result).map_err(|reason| format!("results[{}]: {reason}", number + 1))
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(Article {
        metadata: article,
        results,
        body,
    })
}

fn parse_result(result: &Yaml<'_>) -> Result<Map<String, Value>, String> {
    let Yaml::Mapping(result) = result else {
        return Err("expected a mapping".into());
    };
    let result = project(result, RESULT_FIELDS)?;
    for field in ["baseline", "effect", "certainty"] {
        required(&result, field)?;
    }
    nonempty_string(&result, "claim_id")?;
    Ok(result)
}

fn required<'a>(fields: &'a Map<String, Value>, field: &str) -> Result<&'a Value, String> {
    fields.get(field).ok_or_else(|| format!("missing {field}"))
}

fn nonempty_string(fields: &Map<String, Value>, field: &str) -> Result<(), String> {
    required(fields, field)?
        .as_str()
        .filter(|value| !value.trim().is_empty())
        .map(|_| ())
        .ok_or_else(|| format!("{field} must be a nonempty string"))
}

fn project(map: &saphyr::Mapping<'_>, fields: &[&str]) -> Result<Map<String, Value>, String> {
    fields
        .iter()
        .filter_map(|&key| map.get(&yaml_key(key)).map(|value| (key, value)))
        .map(|(key, value)| {
            json_value(value)
                .map(|value| (key.to_owned(), value))
                .map_err(|reason| format!("{key}: {reason}"))
        })
        .collect()
}

fn yaml_key(key: &str) -> Yaml<'_> {
    Yaml::Value(Scalar::String(key.into()))
}

fn json_value(node: &Yaml<'_>) -> Result<Value, &'static str> {
    match node {
        Yaml::Value(Scalar::Null) => Ok(Value::Null),
        Yaml::Value(Scalar::Boolean(value)) => Ok(Value::from(*value)),
        Yaml::Value(Scalar::Integer(value)) => Ok(Value::from(*value)),
        Yaml::Value(Scalar::FloatingPoint(value)) => serde_json::Number::from_f64(value.0)
            .map(Value::Number)
            .ok_or("number is not finite"),
        Yaml::Value(Scalar::String(value)) => Ok(Value::from(value.as_ref())),
        Yaml::Sequence(values) => values.iter().map(json_value).collect(),
        Yaml::Mapping(map) => map
            .iter()
            .map(|(key, value)| {
                let key = key.as_str().ok_or("mapping keys must be strings")?;
                Ok((key.to_owned(), json_value(value)?))
            })
            .collect::<Result<Map<_, _>, _>>()
            .map(Value::Object),
        _ => Err("unsupported YAML value"),
    }
}
