use pg_worker_core::{
    entropy::{check, Baseline},
    entropy_git::Git,
};
use serde_json::{json, Value};
use std::{env, fs};
fn run() -> Result<Value, Box<dyn std::error::Error>> {
    let mut base = "origin/main".to_string();
    let mut head = "HEAD".to_string();
    let mut body = String::new();
    let mut baseline = false;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--baseline" {
            baseline = true;
            continue;
        }
        let value = args.next().ok_or("missing option value")?;
        match arg.as_str() {
            "--base" => base = value,
            "--head" => head = value,
            "--body-file" => body = fs::read_to_string(value)?,
            _ => return Err("unknown entropy option".into()),
        }
    }
    let git = Git {
        root: env::current_dir()?,
        binary: env::var_os("PG_ENTROPY_GIT").unwrap_or_else(|| "git".into()),
    };
    if baseline {
        let inventory = git.inventory(&git.revision(&head)?)?;
        return Ok(serde_json::to_value(Baseline {
            test_source_ratio: inventory.ratio(),
            ..Baseline::default()
        })?);
    }
    let input = git.input(&base, &head, body)?;
    let findings = check(&input);
    let added = input.changes.iter().map(|c| c.added).sum::<u64>();
    let deleted = input.changes.iter().map(|c| c.deleted).sum::<u64>();
    Ok(
        json!({"ok":findings.is_empty(),"added":added,"deleted":deleted,
        "net_loc":i128::from(added)-i128::from(deleted),"test_source_ratio":input.current.ratio(),
        "classes":input.current.classes,"week_classes":input.week.classes,"findings":findings}),
    )
}
fn main() {
    match run() {
        Ok(report) => {
            println!("{report}");
            if report["ok"] == false {
                std::process::exit(1);
            }
        }
        Err(_) => {
            eprintln!("entropy evidence unavailable: require valid baseline, commits and seven-day history");
            std::process::exit(2);
        }
    }
}
