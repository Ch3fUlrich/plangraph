use crate::executor::{strings, Executor};
use serde_json::Value;
use std::path::PathBuf;
pub fn main(args: &[String]) -> i32 {
    let error = |message: &str| crate::cli::error("inreview-backfill", message);
    let values = [
        "--items",
        "--pgk",
        "--project",
        "--repo",
        "--ref",
        "--log",
        "--out",
        "--out-missing",
        "--go",
        "--rows",
        "--runs-dir",
        "--writer-family",
        "--token-dir",
        "--url",
    ];
    let opts = match crate::cli::options(args, &values, &["--apply", "--dry-run", "--help", "-h"]) {
        Ok(o) => o,
        Err(e) => return error(&e),
    };
    if opts.contains_key("--help") || opts.contains_key("-h") {
        print!("{}", crate::cli::help("inreview-backfill"));
        return 0;
    }
    let path = |k: &str, default: PathBuf| opts.get(k).map_or(default, PathBuf::from);
    let repo = path("--repo", crate::home().join("code/plangraph"));
    let log = path("--log", crate::data_home().join("docs/decisions-log.md"));
    let pgk = path(
        "--pgk",
        std::env::var_os("PG_PGK").map_or("pgk.sh".into(), Into::into),
    );
    let out = path("--out", "inreview-backfill.tsv".into());
    let missing = path("--out-missing", "inreview-backfill-missing.tsv".into());
    if crate::paths::resolve(&out).ok() == crate::paths::resolve(&missing).ok() {
        return error("output paths must differ");
    }
    let authorized =
        opts.contains_key("--apply") && opts.contains_key("--go") && opts.contains_key("--rows");
    if authorized && !crate::handoff::re(r"^D-\d+$").is_match(&opts["--go"]) {
        return error("--go must be a D-id");
    }
    let mut runner = crate::executor::Local;
    let mut result = || -> Result<_, String> {
        let text = if let Some(p) = opts.get("--items") {
            std::fs::read_to_string(p).map_err(|e| e.to_string())?
        } else {
            let o = runner.run(
                "bash",
                &strings(&[&crate::paths::display(&pgk), "snapshot", "--json"]),
                120,
                false,
            )?;
            if o.code != 0 {
                return Err(format!("keeper snapshot rc={}", o.code));
            }
            o.stdout
        };
        let payload: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
        let list = if payload.is_object() {
            &payload["items"]
        } else {
            &payload
        };
        let project = opts.get("--project").map_or("plangraph", String::as_str);
        let items: Vec<_> = list
            .as_array()
            .ok_or("snapshot items is not a list")?
            .iter()
            .filter(|i| {
                i["state"].as_str().unwrap_or("in_review") == "in_review"
                    && i["key"]
                        .as_str()
                        .is_some_and(|k| k.split(':').next() == Some(project))
            })
            .cloned()
            .collect();
        let reference = opts.get("--ref").map_or("main", String::as_str);
        let output = runner.run(
            "git",
            &strings(&[
                "-C",
                &repo.to_string_lossy(),
                "log",
                "--first-parent",
                "--merges",
                "--format=%H%x00%P%x00%(trailers:key=Plan-Item,valueonly)%x00%s%x00",
                "--end-of-options",
                reference,
                "--",
            ]),
            120,
            false,
        )?;
        if output.code != 0 {
            return Err(format!("git log rc={}", output.code));
        }
        let merged = crate::backfill::merged_items(&output.stdout);
        let text = std::fs::read_to_string(&log).map_err(|e| e.to_string())?;
        let report = crate::backfill::report(&items, &merged, &text);
        std::fs::write(&out, crate::backfill::tsv(&report.rows)).map_err(|e| e.to_string())?;
        std::fs::write(&missing, crate::backfill::tsv(&report.missing))
            .map_err(|e| e.to_string())?;
        Ok(report)
    };
    let report = match result() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("inreview-backfill: {e}");
            return 1;
        }
    };
    println!("{}", report.summary());
    let options = crate::backfill_apply::Options {
        apply: opts.contains_key("--apply"),
        go: opts.get("--go").cloned(),
        rows: opts.get("--rows").map(Into::into),
        dry_run: opts.contains_key("--dry-run"),
        runs: opts.get("--runs-dir").map(Into::into),
        writer_family: opts.get("--writer-family").cloned(),
        token_dir: opts.get("--token-dir").map(Into::into),
        url: opts.get("--url").cloned(),
        repo,
        pgk,
        python: std::env::var("PG_FLEET_PYTHON").unwrap_or_else(|_| {
            crate::process::command("python3")
                .get_program()
                .to_string_lossy()
                .into_owned()
        }),
    };
    let (code, text) = crate::backfill_apply::apply(&options, &report.rows, &mut runner);
    print!("{text}");
    code
}
