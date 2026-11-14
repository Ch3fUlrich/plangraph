//! Complete, deterministic primary classification of historical refusal reports.
use crate::backfill_paths::{classify, PATHS};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Serialize)]
pub struct ClassPlan {
    pub class: String,
    pub path: String,
    pub owner: String,
    pub rules: String,
    pub primary_keys: Vec<String>,
    pub secondary_keys: Vec<String>,
}

#[derive(Serialize)]
pub struct Plan {
    pub status: String,
    pub total: usize,
    pub table: Vec<ClassPlan>,
    pub owner_by_key: BTreeMap<String, String>,
    pub followups: Vec<Value>,
    pub pending: Vec<String>,
}

pub fn build(items: &[Value], reports: &[Vec<Value>], expected: usize) -> Result<Plan, String> {
    let mut wanted = BTreeSet::new();
    let mut owners = BTreeMap::new();
    for item in items.iter().filter(|i| i["state"] == "in_review") {
        let key = item["key"].as_str().ok_or("snapshot key missing")?;
        let (project, _) = key.split_once(':').ok_or("project key required")?;
        if ![
            "plangraph",
            "server",
            "autoos",
            "project-alpha",
            "project-beta",
        ]
        .contains(&project)
        {
            return Err("snapshot contains a project outside the D-5572 cohort".into());
        }
        if !wanted.insert(key.to_owned()) {
            return Err("duplicate snapshot key".into());
        }
        owners.insert(key.to_owned(), format!("{project} L1"));
    }
    if wanted.len() != expected || expected == 0 {
        return Err("snapshot does not match the expected in_review cohort".into());
    }
    let mut classes: BTreeMap<String, BTreeSet<usize>> = BTreeMap::new();
    for report in reports {
        let mut seen = BTreeSet::new();
        for row in report {
            let key = row["key"].as_str().ok_or("report key missing")?;
            if !wanted.contains(key) || !seen.insert(key) {
                return Err("unknown or duplicate report key".into());
            }
            if row["status"] != "MISSING" {
                return Err("cohort has eligible or unclassified work; refresh the plan".into());
            }
            let reasons = row["reasons"].as_array().ok_or("report reasons missing")?;
            if reasons.is_empty() {
                return Err("empty refusal reasons".into());
            }
            for reason in reasons {
                let class = classify(reason.as_str().ok_or("invalid refusal reason")?)
                    .ok_or("new refusal class requires a routing path; no silent fallback")?;
                classes.entry(key.into()).or_default().insert(class);
            }
        }
    }
    if classes.keys().cloned().collect::<BTreeSet<_>>() != wanted {
        return Err("some cohort keys have no refusal report".into());
    }
    let mut table: Vec<_> = PATHS
        .iter()
        .map(|p| ClassPlan {
            class: p.class.into(),
            path: p.path.into(),
            owner: p.owner.into(),
            rules: p.rules.into(),
            primary_keys: vec![],
            secondary_keys: vec![],
        })
        .collect();
    for (key, classes) in &classes {
        let primary = *classes.first().ok_or("missing primary class")?;
        table[primary].primary_keys.push(key.clone());
        for &secondary in classes.iter().filter(|&&c| c != primary) {
            table[secondary].secondary_keys.push(key.clone());
        }
    }
    let mut followups = Vec::new();
    for (row, path) in table.iter().zip(PATHS) {
        let projects: BTreeSet<_> = row
            .primary_keys
            .iter()
            .filter_map(|k| k.split_once(':').map(|p| p.0))
            .collect();
        for project in projects {
            let keys: Vec<_> = row
                .primary_keys
                .iter()
                .filter(|k| k.starts_with(&format!("{project}:")))
                .collect();
            followups.push(json!({"key":format!("{project}:INREVIEW-BACKFILL-{}",path.slug),
                "project":project,"id":format!("INREVIEW-BACKFILL-{}",path.slug),"kind":"lane",
                "title":format!("Resolve historical {} refusals",path.class),"owner":format!("{project} L1"),
                "description":format!("{} Keys: {}. Rules: {}",path.path,keys.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "),path.rules),
                "done_criteria":"Every assigned key has a verified verdict through seat_verdict/close-merged or a routed release/cancel disposition; no hand verdicts.",
                "depends_on":["plangraph:INREVIEW-BACKFILL-CLASS-PLAN"],"review_tier":"standard"}));
        }
    }
    Ok(Plan {
        status: "PROPOSED_UNRULED".into(),
        total: wanted.len(),
        table,
        owner_by_key: owners,
        followups,
        pending: vec![
            "Routing D-id bound to the generated table".into(),
            "Independent plan review by another model family with ACCEPT".into(),
            "Keeper attachment and follow-up definitions/gate edges (orchestrator only)".into(),
        ],
    })
}

pub fn markdown(plan: &Plan) -> String {
    let mut text = format!("# Historical review refusal plan\n\nStatus: {}. Cohort: {} keys.\n\n| Class | Path | Owner | Rules | Primary keys | Secondary keys |\n|---|---|---|---|---|---|\n",plan.status,plan.total);
    let clean = |s: &str| s.replace('|', "/").replace(['\r', '\n'], " ");
    for row in &plan.table {
        text.push_str(&format!(
            "| {} | {} | {} | {} | {} | {} |\n",
            clean(&row.class),
            clean(&row.path),
            clean(&row.owner),
            clean(&row.rules),
            row.primary_keys.join(", "),
            row.secondary_keys.join(", ")
        ));
    }
    text.push_str("\nPrimary classes follow table order; every key occurs once in that column. Secondary classes do not create duplicate assignments. Project L1 owners are listed in the JSON companion.\n\nPending:\n");
    for pending in &plan.pending {
        text.push_str(&format!("- {pending}\n"));
    }
    text
}
