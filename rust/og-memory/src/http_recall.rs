//! Approved companion mapping; never falls back to legacy projections.
use super::{Http, Request};
use crate::{
    io::{Error, Receipt, Saved, Snapshot, Transport},
    Envelope, Source, Validated,
};
use serde_json::{json, Value};
use std::{collections::BTreeSet, sync::OnceLock};

#[path = "approved_schema.rs"]
mod approved_schema;
const SCHEMA: &str = approved_schema::SCHEMA;
static CAPS: OnceLock<crate::Capabilities> = OnceLock::new();
const RESOLVE: &str = "query memorySnapshot { match { $n: Project } return { $n.slug } }";

const RECORDS: &str = "query memoryRecallRecords($project: String) {
  match { $n: MemoryRecord }
  where { $n.project == $project }
  return { $n.project, $n.kind, $n.slug, $n.content, $n.version,
           $n.sourceType, $n.sourcePath, $n.sourceHash, $n.actor, $n.relationsJson }
}";
const STATES: &str = "query memoryRecallStates($project: String) {
  match { $n: MemorySavedState }
  where { $n.project == $project }
  return { $n.project, $n.kind, $n.slug, $n.receiptJson, $n.superseded }
}";

fn text<'a>(row: &'a Value, field: &str) -> Result<&'a str, Error> {
    row.get(field)
        .and_then(Value::as_str)
        .ok_or(Error::Unverified)
}
fn key(row: &Value) -> Result<(&str, &str, &str), Error> {
    Ok((
        text(row, "project")?,
        text(row, "kind")?,
        text(row, "slug")?,
    ))
}
fn rows(response: &Value) -> Result<(&str, &[Value]), Error> {
    let commit = text(response, "graph_commit_id")?;
    let rows = response["rows"].as_array().ok_or(Error::Unverified)?;
    if commit.is_empty()
        || response["row_count"].as_u64() != Some(rows.len() as u64)
        || response
            .get("next_page_token")
            .is_some_and(|v| !v.is_null())
    {
        return Err(Error::Unverified);
    }
    Ok((commit, rows))
}

impl Transport for Http {
    fn read(&mut self, project: &str) -> Result<Snapshot, Error> {
        if !crate::slug_ok(project) || project.starts_with("routing-") {
            return Err(Error::InvalidScope);
        }
        let resolved = self.query(&Request {
            query: RESOLVE,
            params: json!({}),
        })?;
        let (commit, _) = rows(&resolved)?;
        // snapshot is a top-level HTTP request field, never a GQ parameter.
        let records = self.post_snapshot(RECORDS, project, commit)?;
        let (record_commit, records) = rows(&records)?;
        if record_commit != commit {
            return Err(Error::Unverified);
        }
        let states = self.post_snapshot(STATES, project, commit)?;
        let (state_commit, states) = rows(&states)?;
        if state_commit != commit || records.len() != states.len() {
            return Err(Error::Unverified);
        }
        let caps = CAPS.get_or_init(|| crate::Capabilities::from_schema(SCHEMA));
        let mut seen = BTreeSet::new();
        let mut facts = Vec::new();
        for row in records {
            let identity = key(row)?;
            if identity.0 != project || !seen.insert(identity) {
                return Err(Error::Unverified);
            }
            let matches = states
                .iter()
                .filter(|s| key(s).ok() == Some(identity))
                .collect::<Vec<_>>();
            if matches.len() != 1 {
                return Err(Error::Unverified);
            }
            let state = matches[0];
            let relations_text = text(row, "relationsJson")?;
            let envelope = Envelope {
                version: row["version"]
                    .as_u64()
                    .and_then(|v| v.try_into().ok())
                    .ok_or(Error::Unverified)?,
                project: identity.0.into(),
                kind: identity.1.into(),
                slug: identity.2.into(),
                content: text(row, "content")?.into(),
                source: Source {
                    r#type: text(row, "sourceType")?.into(),
                    path: text(row, "sourcePath")?.into(),
                    hash: text(row, "sourceHash")?.into(),
                },
                actor: text(row, "actor")?.into(),
                relations: serde_json::from_str(relations_text).map_err(|_| Error::Unverified)?,
            };
            crate::validate(&envelope, caps).map_err(|_| Error::Unverified)?;
            if serde_json::to_string(&envelope.relations).map_err(|_| Error::Unverified)?
                != relations_text
            {
                return Err(Error::Unverified);
            }
            let receipt = text(state, "receiptJson")?;
            let parsed: Value = serde_json::from_str(receipt).map_err(|_| Error::Unverified)?;
            if text(&parsed, "graph_commit_id")?.is_empty()
                || !parsed["created_at"].is_number()
                || !parsed["graph_manifest_version"].is_number()
            {
                return Err(Error::Unverified);
            }
            facts.push(Saved {
                envelope,
                receipt: Receipt(receipt.into()),
                superseded: state["superseded"].as_bool().ok_or(Error::Unverified)?,
            });
        }
        Ok(Snapshot {
            graph_commit: commit.into(),
            facts,
        })
    }

    fn commit(&mut self, _: &Validated, _: &str) -> Result<Receipt, Error> {
        // Recall-only cut: never publish without the receipt follow-up protocol.
        Err(Error::Unavailable)
    }
}
