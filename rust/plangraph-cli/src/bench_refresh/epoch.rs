use super::Observation;
use std::io::{Cursor, Read};

const CSV_LIMIT: u64 = 8 * 1024 * 1024;

struct Export {
    file: &'static str,
    origin: &'static str,
    url: &'static str,
    score: &'static str,
    cost: &'static str,
    tokens: &'static str,
    steps: &'static str,
}

const EXPORTS: [Export; 3] = [
    Export {
        file: "deepswe_external.csv",
        origin: "deepswe",
        url: "https://epoch.ai/benchmarks/deepswe",
        score: "Pass@1",
        cost: "Mean cost (USD)",
        tokens: "Mean output tokens",
        steps: "Mean agent steps",
    },
    Export {
        file: "cursorbench_external.csv",
        origin: "cursorbench",
        url: "https://cursor.com/cursorbench",
        score: "Score",
        cost: "Cost per task",
        tokens: "Tokens per task",
        steps: "Steps per task",
    },
    Export {
        file: "frontierswe_external.csv",
        origin: "frontierswe",
        url: "https://www.frontierswe.com/",
        score: "Score",
        cost: "Average cost (USD)",
        tokens: "",
        steps: "",
    },
];

pub(super) fn observations(body: &[u8]) -> Result<Vec<Observation>, Box<dyn std::error::Error>> {
    let mut archive = zip::ZipArchive::new(Cursor::new(body))?;
    let mut observations = Vec::new();
    for export in EXPORTS {
        let file = match archive.by_name(export.file) {
            Ok(file) => file,
            Err(zip::result::ZipError::FileNotFound) => continue,
            Err(error) => return Err(error.into()),
        };
        if file.size() > CSV_LIMIT {
            return Err("Epoch export exceeds the CSV limit".into());
        }
        let mut csv = csv::Reader::from_reader(file.take(CSV_LIMIT + 1));
        let headers = csv.headers()?.clone();
        if !["Model version", export.score]
            .iter()
            .all(|name| headers.iter().any(|h| h == *name))
        {
            return Err(format!("{} is missing benchmark columns", export.file).into());
        }
        for row in csv.records() {
            let row = row?;
            let field = |key: &str| {
                headers
                    .iter()
                    .position(|h| h == key)
                    .and_then(|i| row.get(i))
                    .map(str::trim)
                    .filter(|value| !value.is_empty())
            };
            let number = |key| {
                field(key)
                    .and_then(|s| s.parse::<f64>().ok())
                    .filter(|n| n.is_finite() && *n >= 0.0)
            };
            let Some(version) = field("Model version") else {
                continue;
            };
            let (model, suffix) = version.rsplit_once('_').unwrap_or((version, ""));
            let effort = field("Reasoning effort")
                .or_else(|| field("Reasoning level"))
                .unwrap_or(suffix)
                .to_ascii_lowercase();
            let effort = match effort.as_str() {
                "extra high" | "extra-high" => "xhigh",
                "unknown" | "n/a" => "",
                value => value,
            };
            let harness = field("Harness").unwrap_or(if export.origin == "cursorbench" {
                "cursor"
            } else {
                "unreported"
            });
            observations.push(Observation {
                model: model.into(),
                effort: effort.into(),
                harness: harness.into(),
                origin: export.origin,
                score: number(export.score)
                    .filter(|score| *score <= 1.0)
                    .map(|score| score * 100.0),
                score_se: number("95% CI half-width").map(|width| width * 100.0 / 1.96),
                runs: field("Runs").and_then(|value| value.parse::<u32>().ok()),
                steps: number(export.steps),
                cost: number(export.cost),
                tokens: number(export.tokens),
                source_url: field("Source")
                    .filter(|source| source.starts_with("https://"))
                    .unwrap_or(export.url)
                    .into(),
                source_record: field("Name").unwrap_or(version).into(),
                index_version: None,
                measured_at: field("Date of evaluation")
                    .or_else(|| field("Started at"))
                    .filter(|value| {
                        chrono::NaiveDate::parse_from_str(value, "%Y-%m-%d").is_ok()
                            || chrono::DateTime::parse_from_rfc3339(value).is_ok()
                    })
                    .map(str::to_owned),
            });
        }
    }
    if observations.is_empty() {
        return Err("Epoch archive has no permitted benchmark observations".into());
    }
    Ok(observations)
}
