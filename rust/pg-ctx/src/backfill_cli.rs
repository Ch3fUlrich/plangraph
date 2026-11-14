use crate::keeper_backfill_plan::{build, markdown};
use serde_json::Value;

pub fn execute(args: &[String]) -> Result<String, String> {
    let Some((snapshot, tail)) = args.split_first() else {
        return Err("backfill-plan SNAPSHOT REPORT... [--expected COUNT] [--markdown]".into());
    };
    let read = |path: &str| -> Result<Value, String> {
        serde_json::from_slice(&std::fs::read(path).map_err(|_| "input unavailable")?)
            .map_err(|_| "input JSON invalid".into())
    };
    let snapshot = read(snapshot)?;
    let items = snapshot["items"]
        .as_array()
        .ok_or("snapshot items missing")?;
    let mut reports = Vec::new();
    let mut expected = 210;
    let mut as_markdown = false;
    let mut tail = tail.iter();
    while let Some(arg) = tail.next() {
        match arg.as_str() {
            "--expected" => {
                expected = tail
                    .next()
                    .ok_or("expected count missing")?
                    .parse()
                    .map_err(|_| "expected count invalid")?
            }
            "--markdown" => as_markdown = true,
            option if option.starts_with('-') => return Err("unknown backfill option".into()),
            path => reports.push(
                read(path)?
                    .as_array()
                    .ok_or("report must be an array")?
                    .clone(),
            ),
        }
    }
    let plan = build(items, &reports, expected)?;
    if as_markdown {
        Ok(markdown(&plan))
    } else {
        serde_json::to_string_pretty(&plan).map_err(|_| "plan encoding failed".into())
    }
}
