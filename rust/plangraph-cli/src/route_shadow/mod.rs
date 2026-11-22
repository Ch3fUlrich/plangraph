//! Read-only comparison of recorded static routing and benchmark choices.
mod input;

use chrono::{NaiveDate, Utc};
use input::{Attempt, Bench, Identity};
use serde::Serialize;
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Write,
    path::{Path, PathBuf},
};

const USAGE: &str = "usage: plangraph route-shadow --since YYYY-MM-DD [--catalog PATH] [--attempts FILE] [--sample-floor N] [--json | --text]";
const SAMPLE_FLOOR: usize = 10;

#[derive(Serialize)]
struct Report<'a> {
    since: NaiveDate,
    generated_at: chrono::DateTime<Utc>,
    sample_floor: usize,
    bench_source: &'static str,
    input_n: usize,
    excluded_pinned: usize,
    excluded_unclassified: usize,
    verification_unavailable_cards: usize,
    rows: Vec<ClassRow<'a>>,
}

#[derive(Serialize)]
struct ClassRow<'a> {
    task_class: &'a str,
    role: &'a str,
    n: usize,
    below_sample_floor: bool,
    utility_complete_n: usize,
    fixed: Arm<'a>,
    utility: Arm<'a>,
}

#[derive(Serialize)]
struct Arm<'a> {
    leg: Option<&'a str>,
    picks: usize,
    decisions_n: usize,
    observed_n: usize,
    first_pass_n: usize,
    first_pass_rate: Option<f64>,
    verified_n: usize,
    verification_unknown_n: usize,
    cost_known_n: usize,
    cost_per_verified_success_usd: Option<f64>,
}

fn arm<'a>(
    votes: &[&'a str],
    observations: &[&'a Attempt],
    first_runs: &BTreeSet<&str>,
) -> Arm<'a> {
    let mut counts = BTreeMap::new();
    for &leg in votes {
        *counts.entry(leg).or_insert(0) += 1;
    }
    let (leg, picks) = counts
        .into_iter()
        .max_by(|(a, a_n), (b, b_n)| a_n.cmp(b_n).then_with(|| b.cmp(a)))
        .map_or((None, 0), |(leg, n)| (Some(leg), n));
    let mut result = Arm {
        leg,
        picks,
        decisions_n: votes.len(),
        observed_n: 0,
        first_pass_n: 0,
        first_pass_rate: None,
        verified_n: 0,
        verification_unknown_n: 0,
        cost_known_n: 0,
        cost_per_verified_success_usd: None,
    };
    let mut first_passes = 0;
    let mut total_cost = 0.0;
    for row in observations
        .iter()
        .filter(|row| leg.is_some() && row.leg.as_deref() == leg)
    {
        result.observed_n += 1;
        if first_runs.contains(row.attempt_id.as_str()) {
            result.first_pass_n += 1;
            if row.exit_class == "ok" {
                first_passes += 1;
            }
        }
        if row.exit_class == "ok" {
            match row.verified {
                Some(true) => result.verified_n += 1,
                Some(false) => (),
                None => result.verification_unknown_n += 1,
            }
        }
        if let Some(cost) = row.cost.filter(|cost| *cost >= 0.0) {
            result.cost_known_n += 1;
            total_cost += cost;
        }
    }
    if result.first_pass_n > 0 {
        result.first_pass_rate = Some(first_passes as f64 / result.first_pass_n as f64);
    }
    if result.verified_n > 0
        && result.verification_unknown_n == 0
        && result.cost_known_n == result.observed_n
        && total_cost.is_finite()
    {
        result.cost_per_verified_success_usd = Some(total_cost / result.verified_n as f64);
    }
    result
}

fn identity(row: &Attempt) -> Option<Identity<'_>> {
    Some(Identity {
        model: row.requested_model.as_deref()?,
        harness: row.harness.as_deref()?,
    })
}

fn compare<'a>(rows: &'a [Attempt], bench: &Bench, floor: usize) -> Vec<ClassRow<'a>> {
    let mut first_by_card = BTreeMap::new();
    let mut identities = BTreeMap::<_, BTreeSet<_>>::new();
    let mut classes = BTreeMap::<_, Vec<_>>::new();
    for row in rows {
        if let Some(card) = row.card_key.as_deref() {
            first_by_card
                .entry((row.role.as_str(), card))
                .or_insert(row.attempt_id.as_str());
        }
        if let (Some(leg), Some(identity)) = (row.leg.as_deref(), identity(row)) {
            identities
                .entry((leg, row.effort.as_deref()))
                .or_default()
                .insert(identity);
        }
        if let Some(class) = row.task_class.as_deref().filter(|class| !class.is_empty()) {
            if !row.route.pin {
                classes
                    .entry((class, row.role.as_str()))
                    .or_default()
                    .push(row);
            }
        }
    }
    let first_runs = first_by_card.into_values().collect();
    classes
        .into_iter()
        .map(|((task_class, role), observations)| {
            let fixed = observations
                .iter()
                .filter_map(|row| row.route.baselines.static_v0.as_deref())
                .collect::<Vec<_>>();
            let mut utility = Vec::new();
            let mut utility_complete_n = 0;
            for row in &observations {
                let ranked = row
                    .route
                    .top5
                    .iter()
                    .filter_map(|candidate| {
                        let key = (candidate.leg.as_str(), candidate.effort.as_deref());
                        let observed = (row.leg.as_deref() == Some(key.0)
                            && row.effort.as_deref() == key.1)
                            .then(|| identity(row))
                            .flatten();
                        let evidence = observed.as_ref().or_else(|| {
                            identities
                                .get(&key)
                                .filter(|ids| ids.len() == 1)
                                .and_then(BTreeSet::first)
                        });
                        Some((bench.utility(candidate, evidence)?, candidate.leg.as_str()))
                    })
                    .collect::<Vec<_>>();
                if let Some((_, leg)) = ranked
                    .iter()
                    .copied()
                    .max_by(|(a, a_id), (b, b_id)| a.total_cmp(b).then_with(|| b_id.cmp(a_id)))
                {
                    utility.push(leg);
                    utility_complete_n += usize::from(ranked.len() == row.route.eligible_n);
                }
            }
            ClassRow {
                task_class,
                role,
                n: observations.len(),
                below_sample_floor: observations.len() < floor,
                utility_complete_n,
                fixed: arm(&fixed, &observations, &first_runs),
                utility: arm(&utility, &observations, &first_runs),
            }
        })
        .collect()
}

fn write_text(out: &mut dyn Write, report: &Report<'_>) -> std::io::Result<()> {
    writeln!(out, "Since {} UTC | generated {} | sample floor {} | benchmarks {} | inputs {} | pinned {} | unclassified {} | verification unavailable for {} cards",
        report.since, report.generated_at.to_rfc3339(), report.sample_floor, report.bench_source,
        report.input_n, report.excluded_pinned, report.excluded_unclassified, report.verification_unavailable_cards)?;
    writeln!(out, "Task class\tRole\tN\tSample\tFixed leg\tFixed picks/scored\tFixed observed\tFixed first pass (n)\tFixed USD/verified (n)\tUtility leg\tUtility picks/scored\tUtility observed\tUtility first pass (n)\tUtility USD/verified (n)\tFully scored runs")?;
    if report.rows.is_empty() {
        writeln!(out, "No classified, unpinned attempts in this window.")?;
    }
    for row in &report.rows {
        let sample = if row.below_sample_floor {
            "below floor"
        } else {
            "at floor or above"
        };
        write!(out, "{}\t{}\t{}\t{sample}", row.task_class, row.role, row.n)?;
        for arm in [&row.fixed, &row.utility] {
            let ratio = arm
                .first_pass_rate
                .map_or_else(|| "—".into(), |rate| format!("{:.1}%", 100.0 * rate));
            let cost = arm
                .cost_per_verified_success_usd
                .map_or_else(|| "—".into(), |cost| format!("{cost:.3}"));
            write!(
                out,
                "\t{}\t{}/{}\t{}\t{ratio} ({})\t{cost} ({})",
                arm.leg.unwrap_or("—"),
                arm.picks,
                arm.decisions_n,
                arm.observed_n,
                arm.first_pass_n,
                arm.verified_n
            )?;
        }
        writeln!(out, "\t{}", row.utility_complete_n)?;
    }
    Ok(())
}

/// Print the comparison using the configured Keeper reader or a read-only export.
pub fn cli(args: &[String]) -> i32 {
    if args == ["--help"] || args == ["-h"] {
        println!("{USAGE}");
        return 0;
    }
    let result = (|| {
        let home = std::env::home_dir().ok_or("home unavailable")?;
        let settings = if args.iter().any(|arg| arg == "--attempts") {
            std::env::var("PLANGRAPH_CATALOG_DB")
                .ok()
                .map(|path| BTreeMap::from([("PLANGRAPH_CATALOG_DB".into(), path)]))
                .unwrap_or_default()
        } else {
            crate::env::load(&crate::env::default_path()?, std::env::vars())?
        };
        run(&home, &settings, args, &mut std::io::stdout().lock())
    })();
    match result {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("route-shadow: {error}");
            2
        }
    }
}

fn run(
    home: &Path,
    env: &BTreeMap<String, String>,
    args: &[String],
    out: &mut dyn Write,
) -> Result<(), String> {
    let (mut date, mut catalog, mut file, mut floor, mut json) = (None, None, None, None, None);
    let mut args = args.iter().map(String::as_str);
    while let Some(arg) = args.next() {
        match arg {
            "--since" if date.is_none() => {
                date = Some(
                    NaiveDate::parse_from_str(args.next().ok_or(USAGE)?, "%Y-%m-%d")
                        .map_err(|_| "--since must be a date in YYYY-MM-DD format")?,
                );
            }
            "--catalog" if catalog.is_none() => {
                catalog = Some(PathBuf::from(args.next().ok_or(USAGE)?))
            }
            "--attempts" if file.is_none() => file = Some(PathBuf::from(args.next().ok_or(USAGE)?)),
            "--sample-floor" if floor.is_none() => {
                floor = Some(
                    args.next()
                        .ok_or(USAGE)?
                        .parse::<usize>()
                        .ok()
                        .filter(|n| *n > 0)
                        .ok_or("--sample-floor must be a positive integer")?,
                );
            }
            "--json" | "--text" if json.is_none() => json = Some(arg == "--json"),
            _ => return Err(USAGE.into()),
        }
    }
    let since = date.ok_or(USAGE)?;
    let start = since.and_hms_opt(0, 0, 0).expect("midnight").and_utc();
    let now = Utc::now();
    if start.timestamp() < 0 || start > now {
        return Err("--since must be between 1970-01-01 and today UTC".into());
    }
    let catalog = catalog
        .or_else(|| {
            env.get("PLANGRAPH_CATALOG_DB").map(|path| {
                path.strip_prefix("~/")
                    .map_or_else(|| PathBuf::from(path), |rest| home.join(rest))
            })
        })
        .unwrap_or_else(|| home.join(".cache/plangraph/spawner_catalog.db"));
    let bench = Bench::open(&catalog).map_err(|_| "cannot read benchmark catalog")?;
    let (mut rows, verification_unavailable_cards) =
        input::attempts(env, file.as_deref(), start, now)?;
    rows.retain(|row| row.started_at >= start);
    rows.sort_unstable_by(|a, b| {
        a.started_at
            .cmp(&b.started_at)
            .then(a.attempt_id.cmp(&b.attempt_id))
    });
    let mut ids = BTreeSet::new();
    if rows.iter().any(|row| !ids.insert(&row.attempt_id)) {
        return Err("duplicate attempt_id in route-shadow input".into());
    }
    let sample_floor = floor.unwrap_or(SAMPLE_FLOOR);
    let report = Report {
        since,
        generated_at: now,
        sample_floor,
        bench_source: bench.source,
        input_n: rows.len(),
        excluded_pinned: rows.iter().filter(|row| row.route.pin).count(),
        excluded_unclassified: rows
            .iter()
            .filter(|row| !row.route.pin && row.task_class.as_deref().is_none_or(str::is_empty))
            .count(),
        verification_unavailable_cards,
        rows: compare(&rows, &bench, sample_floor),
    };
    if json.unwrap_or(false) {
        serde_json::to_writer(&mut *out, &report).map_err(|e| e.to_string())?;
        writeln!(out).map_err(|e| e.to_string())
    } else {
        write_text(out, &report).map_err(|e| e.to_string())
    }
}
