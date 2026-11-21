mod epoch;
mod openrouter;

use rusqlite::{params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use serde_json::json;
use std::path::{Path, PathBuf};
use std::time::Duration;

type Error = Box<dyn std::error::Error>;
type Result<T> = std::result::Result<T, Error>;

const SCHEMA: &str = include_str!("../../../src/plangraph/routing_support/catalog_schema.sql");
const EPOCH: &str = "https://epoch.ai/data/benchmark_data.zip";
const OPENROUTER: &str = "https://openrouter.ai/api/v1/models";
const EPOCH_ATTRIBUTION: &str = "Epoch AI, Capabilities & benchmarking, https://epoch.ai/benchmarks/use-this-data (CC BY 4.0). External benchmark results retain their publishers’ licenses.";

struct Observation {
    model: String,
    effort: String,
    harness: String,
    origin: &'static str,
    score: Option<f64>,
    score_se: Option<f64>,
    runs: Option<u32>,
    steps: Option<f64>,
    cost: Option<f64>,
    tokens: Option<f64>,
    source_url: String,
    source_record: String,
    index_version: Option<String>,
    measured_at: Option<String>,
}

#[derive(Default, PartialEq, Eq)]
struct Validators {
    etag: Option<String>,
    last_modified: Option<String>,
    revision: Option<i64>,
}

fn validators(db: &Connection, source: &str, url: &str) -> Result<Validators> {
    let exists = db
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE name='catalog_benchmark_refresh_sources'",
            [],
            |_| Ok(()),
        )
        .optional()?
        .is_some();
    if !exists {
        return Ok(Validators::default());
    }
    Ok(db.query_row(
            "SELECT CASE WHEN url=?2 THEN etag END, CASE WHEN url=?2 THEN last_modified END, revision
             FROM catalog_benchmark_refresh_sources WHERE source=?1",
            params![source, url], |row| Ok(Validators { etag: row.get(0)?, last_modified: row.get(1)?, revision: row.get(2)? }),
    ).optional()?.unwrap_or_default())
}

fn download(
    agent: &ureq::Agent,
    url: &str,
    known: &Validators,
) -> Result<Option<(Validators, Vec<u8>)>> {
    let uri: ureq::http::Uri = url.parse()?;
    let local = uri
        .host()
        .is_some_and(|host| matches!(host, "localhost" | "127.0.0.1" | "[::1]"));
    if uri.host().is_none()
        || !matches!(uri.scheme_str(), Some("https"))
            && !(local && uri.scheme_str() == Some("http"))
    {
        return Err("benchmark source must use HTTPS or loopback HTTP".into());
    }
    let mut request = agent.get(url);
    for (header, value) in [
        ("If-None-Match", &known.etag),
        ("If-Modified-Since", &known.last_modified),
    ] {
        if let Some(value) = value {
            request = request.header(header, value);
        }
    }
    let mut response = request.call()?;
    match response.status().as_u16() {
        304 if known.etag.is_some() || known.last_modified.is_some() => return Ok(None),
        200 => {}
        status => return Err(format!("benchmark source returned HTTP {status}").into()),
    }
    let header = |name| {
        response
            .headers()
            .get(name)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
    };
    let received = Validators {
        etag: header("etag"),
        last_modified: header("last-modified"),
        revision: None,
    };
    let body = response
        .body_mut()
        .with_config()
        .limit(16 * 1024 * 1024)
        .read_to_vec()?;
    Ok(Some((received, body)))
}

fn write_observations(
    db: &Connection,
    observations: Vec<Observation>,
    checked: &str,
) -> Result<usize> {
    let mut statement = db.prepare_cached(
        "INSERT INTO catalog_benchmark_measurements (model_id, effort, harness, origin, score,
            score_se, n_runs, steps, cost_per_task_usd, tokens_per_task, imputed, source_url,
            source_date, source_record, index_version, measured_at, effort_channel)
         VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,1,?11,?12,?13,?14,?15,
                 CASE WHEN ?2 != '' THEN 'publisher' END)
         ON CONFLICT (model_id, effort, harness, origin) DO UPDATE SET score=excluded.score,
            score_se=excluded.score_se, n_runs=excluded.n_runs, steps=excluded.steps,
            cost_per_task_usd=excluded.cost_per_task_usd, tokens_per_task=excluded.tokens_per_task,
            imputed=excluded.imputed, source_url=excluded.source_url, source_date=excluded.source_date,
            source_record=excluded.source_record, index_version=excluded.index_version,
            measured_at=excluded.measured_at, effort_channel=excluded.effort_channel
         WHERE catalog_benchmark_measurements.source_record IS NULL
            OR catalog_benchmark_measurements.source_url IS excluded.source_url",
    )?;
    let mut written = 0;
    for row in observations {
        written += statement.execute(params![
            row.model,
            row.effort,
            row.harness,
            row.origin,
            row.score,
            row.score_se,
            row.runs,
            row.steps,
            row.cost,
            row.tokens,
            row.source_url,
            checked,
            row.source_record,
            row.index_version,
            row.measured_at
        ])?;
    }
    Ok(written)
}

fn refresh(path: &Path, epoch_url: &str, openrouter_url: &str) -> Result<usize> {
    let endpoints = [
        ("epoch", epoch_url, Some(EPOCH_ATTRIBUTION)),
        ("openrouter", openrouter_url, None),
    ];
    let mut known = [Validators::default(), Validators::default()];
    if path.try_exists()? {
        let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        for (i, (source, url, _)) in endpoints.iter().enumerate() {
            known[i] = validators(&db, source, url)?;
        }
    }
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .http_status_as_error(false)
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(30)))
        .build()
        .into();
    let epoch = download(&agent, epoch_url, &known[0])?;
    let openrouter = download(&agent, openrouter_url, &known[1])?;
    if epoch.is_none() && openrouter.is_none() {
        return Ok(0);
    }
    let epoch_observations = epoch
        .as_ref()
        .map(|(_, body)| epoch::observations(body))
        .transpose()?;
    let models = openrouter
        .as_ref()
        .map(|(_, body)| openrouter::models(body))
        .transpose()?;
    let mut db = Connection::open(path)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute_batch(SCHEMA)?;
    for (i, (source, url, _)) in endpoints.iter().enumerate() {
        if validators(&tx, source, url)? != known[i] {
            return Err(
                "benchmark sources changed during refresh; retry with current validators".into(),
            );
        }
    }
    let date = chrono::Utc::now().date_naive().to_string();
    let mut written = 0;
    if let Some(observations) = epoch_observations {
        written += write_observations(&tx, observations, &date)?;
    }
    if let Some(models) = models {
        written += openrouter::apply_models(&tx, models, openrouter_url, &date)?;
    }
    for ((source, url, attribution), response) in endpoints.into_iter().zip([epoch, openrouter]) {
        if let Some((headers, _)) = response {
            tx.execute(
                "INSERT INTO catalog_benchmark_refresh_sources (source,url,etag,last_modified,attribution)
                 VALUES (?1,?2,?3,?4,?5) ON CONFLICT (source) DO UPDATE SET url=excluded.url,
                    etag=excluded.etag,last_modified=excluded.last_modified,attribution=excluded.attribution,
                    revision=catalog_benchmark_refresh_sources.revision+1",
                params![
                    source,
                    url,
                    headers.etag,
                    headers.last_modified,
                    attribution
                ],
            )?;
        }
    }
    tx.commit()?;
    Ok(written)
}

/// Refresh the benchmark catalog from conditional public reads.
pub fn run(args: &[String]) -> i32 {
    let (mut db, mut epoch_url, mut openrouter_url) = (None, EPOCH, OPENROUTER);
    let (args, remainder) = args.as_chunks::<2>();
    for [flag, value] in args {
        match flag.as_str() {
            "--db" if db.is_none() => db = Some(PathBuf::from(value)),
            "--epoch-url" => epoch_url = value,
            "--openrouter-url" => openrouter_url = value,
            _ => return usage(),
        }
    }
    let Some(db) = db.filter(|_| remainder.is_empty()) else {
        return usage();
    };
    match refresh(&db, epoch_url, openrouter_url) {
        Ok(written) => {
            println!("{}", json!({"rows_written": written}));
            0
        }
        Err(error) => {
            eprintln!("benchmark refresh failed: {error}");
            1
        }
    }
}

fn usage() -> i32 {
    eprintln!("usage: plangraph bench-refresh --db PATH [--epoch-url URL] [--openrouter-url URL]");
    2
}
