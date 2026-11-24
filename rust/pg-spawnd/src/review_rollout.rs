//! Bind the configured lane policy at each real launch, including retries.
use crate::{
    cli_review,
    errors::ToolError,
    placement::{parameter, Args},
};
use pg_worker_core::{
    counted_seat::{sha, Binding, Policy, Selection},
    writer_families::{python_command, writer_families},
};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::Path,
};

fn document(card: &Value) -> bool {
    card["review_document"] == "train_delta"
        && card["witness_required"] == true
        && card["diff_ref"].is_null()
}

pub(crate) fn head(card: &Value) -> &str {
    card["diff_ref"]["head_sha"]
        .as_str()
        .or_else(|| card["diff_ref"]["head"].as_str())
        .or_else(|| card["head_sha"].as_str())
        .filter(|h| sha(h))
        .unwrap_or("")
}

fn check(selection: &Selection, leg: &Value) -> Result<(), String> {
    if leg["id"] != selection.seat.leg {
        return Err(format!(
            "counted review requires leg {}",
            selection.seat.leg
        ));
    }
    Ok(())
}

pub(crate) fn filter(args: &Args, plan: &mut Value) -> Result<(), ToolError> {
    if args.get("role").and_then(Value::as_str) != Some("review") || document(&args["card"]) {
        return Ok(());
    }
    let Some(policy) = Policy::configured().map_err(parameter)? else {
        return Ok(());
    };
    let target = policy.select(head(&args["card"]));
    if cli_review::kind(args).is_none() {
        if let Some(leg) = args.get("leg") {
            check(&target, &json!({"id":leg})).map_err(parameter)?;
        }
    }
    for field in ["legs", "review_seats"] {
        if let Some(legs) = plan[field].as_array_mut() {
            legs.retain(|leg| check(&target, leg).is_ok());
        }
    }
    plan["first_leg"] = plan["legs"][0].clone();
    if let Some(model) = args.get("model") {
        if plan["first_leg"]["model"] != *model {
            return Err(parameter(format!(
                "counted review requires leg {}",
                target.seat.leg
            )));
        }
    }
    if plan["first_leg"].is_null() {
        plan["error"] = json!(format!("counted review requires leg {}", target.seat.leg));
    }
    Ok(())
}

fn measured_lines(repo: &Path, base: &str, head: &str) -> Result<Option<u64>, String> {
    if !sha(base) {
        return Err("counted review requires a pinned diff base".into());
    }
    let bases = [format!("{base}^{{commit}}"), format!("{head}^{{commit}}")];
    let stat = cli_review::git(
        repo,
        &[
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--numstat",
            "--no-renames",
            &bases[0],
            &bases[1],
            "--",
        ],
    )
    .map_err(|_| "counted review pinned diff is unavailable")?;
    // Binary changes are eligible for review but cannot be compared by changed lines.
    Ok(stat.lines().try_fold(0u64, |total, line| {
        line.split('\t')
            .take(2)
            .try_fold(total, |n, count| n.checked_add(count.parse().ok()?))
    }))
}

fn bind(policy: &Policy, request: &Value) -> Result<Binding, String> {
    let card = &request["card"];
    let head = head(card);
    let selection = policy.select(head);
    check(&selection, &request["leg"])?;
    if head.is_empty() || selection.lane.is_none() {
        return Err("counted review requires a pinned head registered to a lane by routing".into());
    }
    let id = request["run_id"]
        .as_str()
        .filter(|id| pg_worker_core::run::record::valid_run_id(id))
        .ok_or("invalid counted run id")?;
    let diff = &card["diff_ref"];
    let repos = crate::prompt::repos();
    let registered = diff["repo_id"].as_str().and_then(|id| repos.get(id));
    let repo = registered
        .map(|r| Path::new(&r.root))
        .or_else(|| diff["repo"].as_str().map(Path::new));
    if !diff["head_sha"]
        .as_str()
        .or_else(|| diff["head"].as_str())
        .is_some_and(|sha| sha == head)
    {
        return Err("counted review requires an exact diff_ref head".into());
    }
    let base = diff["base_sha"]
        .as_str()
        .or_else(|| diff["base"].as_str())
        .unwrap_or("");
    let repo = repo
        .filter(|path| path.is_absolute())
        .ok_or("counted review needs a local pinned diff")?;
    let lines = measured_lines(repo, base, head)?;
    let writers: Vec<_> = request["writer_families"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    let writer_families = writer_families(repo, base, head, &writers, &|| python_command(None))?
        .into_iter()
        .collect();
    Ok(Binding {
        selection,
        head: head.to_owned(),
        run_id: id.to_owned(),
        item_key: request["item_key"]
            .as_str()
            .or_else(|| card["item_key"].as_str())
            .or_else(|| card["item"].as_str())
            .map(str::to_owned),
        diff_lines: lines,
        writer_families,
    })
}

pub(crate) fn launch(
    args: &Args,
    leg: &Value,
    run_id: &str,
    role: &Value,
) -> Result<Option<Binding>, ToolError> {
    let request = json!({"card":args["card"], "role":role, "leg":leg,
        "run_id":run_id, "writer_families":args.get("writer_families")});
    if role != "review" || document(&args["card"]) {
        return Ok(None);
    }
    let Some(policy) = Policy::configured().map_err(parameter)? else {
        return Ok(None);
    };
    let request = crate::launch::with_head_writers(&request, &crate::prompt::repos())?;
    bind(&policy, &request).map(Some).map_err(parameter)
}

pub(crate) fn execute(args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let mut input = Vec::new();
    std::io::stdin()
        .take(262_145)
        .read_to_end(&mut input)
        .map_err(|e| e.to_string())?;
    if input.len() > 262_144 {
        return Err("counted seat input too large".into());
    }
    let request: Value =
        serde_json::from_slice(&input).map_err(|_| "invalid counted seat input")?;
    let result = if let Some(policy) = Policy::configured()? {
        match args {
            [verb] if verb == "select" => json!(policy.select(head(&request))),
            [verb] if verb == "bind" => json!(bind(&policy, &request)?),
            [verb] if verb == "verify" => {
                let binding = serde_json::from_value(request["binding"].clone())
                    .map_err(|_| "missing counted review binding")?;
                policy.verify(
                    &binding,
                    request["head"].as_str().unwrap_or(""),
                    request["run_id"].as_str().unwrap_or(""),
                    request["leg"].as_str().unwrap_or(""),
                )?;
                json!(true)
            }
            _ => return Err("usage: pg-spawnd counted-seat select|bind|verify".into()),
        }
    } else {
        Value::Null
    };
    writeln!(out, "{result}").map_err(|e| e.to_string())
}
