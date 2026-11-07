//! Equality checks used to reconcile committed facts without replaying writes.
use super::{Envelope, Receipt, Snapshot};

pub(super) fn matching(snapshot: &Snapshot, e: &Envelope) -> Option<Receipt> {
    snapshot
        .facts
        .iter()
        .find(|f| !f.superseded && !f.receipt.0.is_empty() && same(&f.envelope, e))
        .map(|f| f.receipt.clone())
}

fn same(a: &Envelope, b: &Envelope) -> bool {
    a.version == b.version
        && a.project == b.project
        && a.kind == b.kind
        && a.slug == b.slug
        && a.content == b.content
        && a.actor == b.actor
        && a.source.r#type == b.source.r#type
        && a.source.path == b.source.path
        && a.source.hash == b.source.hash
        && a.relations.len() == b.relations.len()
        && a.relations
            .iter()
            .zip(&b.relations)
            .all(|(a, b)| a.edge == b.edge && a.target_slug == b.target_slug)
}
