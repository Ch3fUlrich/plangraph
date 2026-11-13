//! Exact decimal daily admission from a complete gateway call-log export.
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub warn_usd: String,
    pub budget_usd: String,
    pub rows_file: PathBuf,
    pub prices: BTreeMap<String, Price>,
    #[serde(default = "max_age")]
    pub max_age_s: i64,
}
fn max_age() -> i64 {
    600
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Price {
    pub input: String,
    pub output: String,
    pub cache_read: String,
}
#[derive(Serialize, Deserialize, Debug)]
pub struct Verdict {
    pub day: String,
    pub generated_at: DateTime<Utc>,
    pub usd: String,
    pub budget: String,
    pub verdict: String,
    pub bad_rows: usize,
    pub unpriced: usize,
    pub truncated: bool,
}
impl Verdict {
    pub fn admits(&self) -> bool {
        self.verdict == "ok" || self.verdict == "near"
    }
}
fn decimal(text: &str) -> Result<Decimal, String> {
    let number = Decimal::from_str_exact(text).map_err(|_| "invalid decimal")?;
    if number.is_sign_negative() {
        return Err("negative decimal".into());
    }
    Ok(number)
}
fn exact_add(a: Decimal, b: Decimal) -> Result<Decimal, String> {
    let scale = a.scale().max(b.scale());
    let align = |number: Decimal| {
        number
            .mantissa()
            .checked_mul(10_i128.pow(scale - number.scale()))
            .ok_or("decimal overflow")
    };
    let mantissa = align(a)?.checked_add(align(b)?).ok_or("decimal overflow")?;
    Decimal::try_from_i128_with_scale(mantissa, scale).map_err(|_| "decimal overflow".into())
}
fn exact_multiply(rate: Decimal, count: u64) -> Result<Decimal, String> {
    let mantissa = rate
        .mantissa()
        .checked_mul(i128::from(count))
        .ok_or("decimal overflow")?;
    Decimal::try_from_i128_with_scale(mantissa, rate.scale()).map_err(|_| "decimal overflow".into())
}
impl Config {
    pub fn read(path: &Path) -> Result<Self, String> {
        crate::atomic::regular(path).map_err(|_| "invalid config")?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if std::fs::metadata(path)
                .map_err(|_| "config unavailable")?
                .permissions()
                .mode()
                & 0o777
                != 0o600
            {
                return Err("config requires mode 0600".into());
            }
        }
        let text = std::fs::read_to_string(path).map_err(|_| "config unavailable")?;
        toml::from_str(&text).map_err(|_| "invalid private config".into())
    }
    fn limits(&self) -> Result<(Decimal, Decimal), String> {
        let warn = decimal(&self.warn_usd)?;
        let budget = decimal(&self.budget_usd)?;
        if warn >= budget
            || budget.is_zero()
            || !(1..=3600).contains(&self.max_age_s)
            || !self.rows_file.is_absolute()
        {
            return Err("invalid private limits or rows path".into());
        }
        Ok((warn, budget))
    }
}
pub fn evaluate(config: &Config, export: &Value, now: DateTime<Utc>) -> Result<Verdict, String> {
    let (warn, budget) = config.limits()?;
    let rows = export
        .get("rows")
        .and_then(Value::as_array)
        .ok_or("rows array required")?;
    let truncated = export
        .get("truncated")
        .and_then(Value::as_bool)
        .ok_or("truncated flag required")?;
    let exported = export
        .get("exported_at")
        .and_then(Value::as_str)
        .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
        .ok_or("export timestamp required")?;
    let day = now.format("%Y-%m-%d").to_string();
    if export.get("day").and_then(Value::as_str) != Some(&day)
        || now.signed_duration_since(exported).num_seconds() > config.max_age_s
        || now.signed_duration_since(exported).num_seconds() < -30
    {
        return Err("stale or wrong-day export".into());
    }
    let mut total = Decimal::ZERO;
    let mut bad_rows = 0;
    let mut unpriced = 0;
    for row in rows {
        let provider = row
            .get("provider")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_ascii_lowercase();
        let timestamp = row.get("timestamp").and_then(timestamp);
        let Some(timestamp) = timestamp else {
            bad_rows += 1;
            continue;
        };
        if timestamp.format("%Y-%m-%d").to_string() != day {
            continue;
        }
        let Some(tokens) = row.get("tokens").and_then(Value::as_object) else {
            bad_rows += 1;
            continue;
        };
        if tokens
            .keys()
            .any(|key| !["in", "out", "cacheRead"].contains(&key.as_str()))
        {
            bad_rows += 1;
            continue;
        }
        let counts = ["in", "out", "cacheRead"].map(|key| {
            tokens
                .get(key)
                .map(|v| v.as_u64().ok_or(()))
                .unwrap_or(Ok(0))
        });
        let [Ok(input), Ok(output), Ok(cache)] = counts else {
            bad_rows += 1;
            continue;
        };
        if input == 0 && output == 0 {
            continue;
        }
        let model = row.get("model").and_then(Value::as_str).unwrap_or("");
        let price = config.prices.get(&format!("{provider}/{model}"));
        let Some(price) = price else {
            unpriced += 1;
            continue;
        };
        let cached = cache.min(input);
        let cost = [
            (input - cached, &price.input),
            (output, &price.output),
            (cached, &price.cache_read),
        ]
        .into_iter()
        .try_fold(Decimal::ZERO, |sum, (count, rate)| {
            exact_add(sum, exact_multiply(decimal(rate)?, count)?)
        })?;
        total = exact_add(total, cost)?;
    }
    let verdict = if truncated {
        "truncated"
    } else if bad_rows > 0 {
        "invalid"
    } else if unpriced > 0 {
        "unpriced"
    } else if total >= budget {
        "over"
    } else if total >= warn {
        "near"
    } else {
        "ok"
    };
    Ok(Verdict {
        day,
        generated_at: now,
        usd: total.normalize().to_string(),
        budget: budget.normalize().to_string(),
        verdict: verdict.into(),
        bad_rows,
        unpriced,
        truncated,
    })
}
fn timestamp(value: &Value) -> Option<DateTime<Utc>> {
    if let Some(text) = value.as_str() {
        if let Ok(stamp) = DateTime::parse_from_rfc3339(text) {
            return Some(stamp.with_timezone(&Utc));
        }
        if let Ok(number) = text.parse::<i64>() {
            return epoch(number);
        }
    }
    value.as_i64().and_then(epoch)
}
fn epoch(number: i64) -> Option<DateTime<Utc>> {
    if number > 100_000_000_000 {
        DateTime::from_timestamp_millis(number)
    } else {
        DateTime::from_timestamp(number, 0)
    }
}
pub fn refresh(config_path: &Path, state: &Path, dry_run: bool) -> Result<Verdict, String> {
    let config = Config::read(config_path)?;
    crate::atomic::regular(&config.rows_file).map_err(|_| "invalid rows file")?;
    // Exporter must replace the complete object atomically. Partial JSON refuses.
    let bytes = std::fs::read(&config.rows_file).map_err(|_| "rows unavailable")?;
    let export: Value = serde_json::from_slice(&bytes).map_err(|_| "invalid rows export")?;
    let verdict = evaluate(&config, &export, Utc::now())?;
    if !dry_run {
        std::fs::create_dir_all(state).map_err(|_| "state unavailable")?;
        let target = state.join("daily-gate.json");
        let _lock = crate::atomic::lock(&target).map_err(|_| "gate busy")?;
        crate::atomic::write(&target, &serde_json::to_vec(&verdict).unwrap())
            .map_err(|_| "gate write failed")?;
        crate::atomic::write(&state.join("daily-gate.status"), line(&verdict).as_bytes())
            .map_err(|_| "status write failed")?;
        match std::fs::remove_file(state.join("daily-gate.unavailable")) {
            Ok(()) => (),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(_) => return Err("status cleanup failed".into()),
        }
    }
    Ok(verdict)
}
pub fn line(verdict: &Verdict) -> String {
    format!(
        "{} verdict={} usd={} budget={} truncated={} unpriced={} bad_rows={}\n",
        verdict.generated_at.to_rfc3339(),
        verdict.verdict,
        verdict.usd,
        verdict.budget,
        verdict.truncated,
        verdict.unpriced,
        verdict.bad_rows
    )
}
pub fn cli(home: &Path, args: &[String]) -> i32 {
    if args
        .first()
        .is_some_and(|s| ["migrate", "overlap-start", "overlap-check"].contains(&s.as_str()))
    {
        return crate::cost_cutover::cli(home, args);
    }
    let state = crate::paths::state(home);
    if args == ["status"] {
        if state.join("daily-gate.unavailable").exists() {
            eprintln!("cost-gate UNAVAILABLE latest refresh failed");
            return 1;
        }
        let verdict = std::fs::read(state.join("daily-gate.json"))
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Verdict>(&bytes).ok());
        if let Some(verdict) = verdict {
            let age = Utc::now()
                .signed_duration_since(verdict.generated_at)
                .num_seconds();
            if (0..=600).contains(&age) && verdict.day == Utc::now().format("%Y-%m-%d").to_string()
            {
                print!("{}", line(&verdict));
                return 0;
            }
        }
        eprintln!("cost-gate UNAVAILABLE stale or missing state");
        return 1;
    }
    let mode = args.first().map(String::as_str);
    if !matches!(mode, Some("refresh" | "admit"))
        || args.len() > 2
        || (args.len() == 2 && args[1] != "--dry-run")
    {
        return 2;
    }
    let config = std::env::var_os("PLANGRAPH_COST_CONFIG")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config/plangraph/daily-gate.toml"));
    match refresh(&config, &state, args.len() == 2) {
        Ok(verdict) => {
            print!("{}", line(&verdict));
            if mode == Some("admit") && !verdict.admits() {
                1
            } else {
                0
            }
        }
        Err(_) => {
            if args.len() != 2 && std::fs::create_dir_all(&state).is_ok() {
                if let Ok(_lock) = crate::atomic::lock(&state.join("daily-gate.json")) {
                    let _ = crate::atomic::write(
                        &state.join("daily-gate.unavailable"),
                        b"latest refresh failed\n",
                    );
                }
            }
            eprintln!("cost-gate UNAVAILABLE private config or complete fresh export required");
            2
        }
    }
}
