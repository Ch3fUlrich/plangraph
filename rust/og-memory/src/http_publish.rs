//! Two conditional writes; the second stores only the first write's real receipt.
use super::{recall, Http, Request};
use crate::{
    io::{Error, Receipt},
    Validated,
};
use serde_json::{json, Value};

const RECORD: &str = "query rememberMemory($project: String, $kind: String, $slug: String,
 $content: String, $version: I32, $sourceType: String, $sourcePath: String,
 $sourceHash: String, $actor: String, $relationsJson: String) {
 insert MemoryRecord { project: $project, kind: $kind, slug: $slug, content: $content,
 version: $version, sourceType: $sourceType, sourcePath: $sourcePath,
 sourceHash: $sourceHash, actor: $actor, relationsJson: $relationsJson }
}";
const STATE: &str = "query saveMemoryState($project: String, $kind: String, $slug: String,
 $receiptJson: String, $superseded: Bool) {
 insert MemorySavedState { project: $project, kind: $kind, slug: $slug,
 receiptJson: $receiptJson, superseded: $superseded }
}";

fn receipt(response: &Value) -> Result<(&str, String), Error> {
    let value = &response["commit"];
    let id = value["graph_commit_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or(Error::Unknown)?;
    if !value["created_at"].is_number() || !value["graph_manifest_version"].is_number() {
        return Err(Error::Unknown);
    }
    Ok((id, value.to_string()))
}

#[cfg(all(test, unix))]
#[path = "http_publish_tests.rs"]
mod tests;

impl Http {
    pub(super) fn publish(&mut self, fact: &Validated, expected: &str) -> Result<Receipt, Error> {
        let e = fact.envelope();
        let params = json!({"project":e.project,"kind":e.kind,"slug":e.slug,
            "content":e.content,"version":e.version,"sourceType":e.source.r#type,
            "sourcePath":e.source.path,"sourceHash":e.source.hash,"actor":e.actor,
            "relationsJson":serde_json::to_string(&e.relations).map_err(|_| Error::Unverified)?});
        let publication = self.mutate(
            &Request {
                query: RECORD,
                params: &params,
            },
            expected,
        )?;
        let (commit, saved_receipt) = receipt(&publication)?;
        // Verify the authoritative record without requiring its not-yet-written state.
        let records = self.post_snapshot(recall::RECORDS, &e.project, commit)?;
        let (read_commit, rows) = recall::rows(&records)?;
        let matches: Vec<_> = rows
            .iter()
            .filter(|r| r["project"] == e.project && r["kind"] == e.kind && r["slug"] == e.slug)
            .collect();
        if read_commit != commit || matches.len() != 1 || matches[0] != &params {
            return Err(Error::Unverified);
        }
        let state = self.mutate(
            &Request {
                query: STATE,
                params: json!({
            "project":e.project,"kind":e.kind,"slug":e.slug,
            "receiptJson":saved_receipt,"superseded":false}),
            },
            read_commit,
        )?;
        let (state_commit, _) = receipt(&state)?;
        let snapshot = self.read_at(&e.project, state_commit)?;
        let saved = snapshot
            .facts
            .iter()
            .find(|f| f.envelope.kind == e.kind && f.envelope.slug == e.slug)
            .ok_or(Error::Unverified)?;
        if saved.superseded
            || saved.receipt.0 != saved_receipt
            || serde_json::to_value(&saved.envelope).ok() != serde_json::to_value(e).ok()
        {
            return Err(Error::Unverified);
        }
        Ok(Receipt(saved_receipt))
    }
}
