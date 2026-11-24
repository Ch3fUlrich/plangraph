//! Several waiting trains become one bigger train (D-6774): the union of their parts, built once on the
//! new main, so the full CI runs once. The parts keep their receipts: no part is reviewed again.
use super::train::{Manifest, PartIn, PartReceipt};
use std::collections::BTreeMap;

/// The parts of every manifest as builder input, each pull request once, in manifest order.
pub fn combine_parts(manifests: &[Manifest]) -> Result<Vec<PartIn>, String> {
    if manifests.len() < 2 {
        return Err("combine: needs at least two trains".into());
    }
    let mut seen: BTreeMap<u64, String> = BTreeMap::new();
    let mut out = Vec::new();
    for m in manifests {
        for p in &m.parts {
            match seen.get(&p.pr) {
                Some(head) if *head == p.head => continue,
                Some(head) => {
                    return Err(format!(
                        "combine: #{} is at {head} in one train and {} in {}",
                        p.pr, p.head, m.build
                    ))
                }
                None => {}
            }
            seen.insert(p.pr, p.head.clone());
            out.push(PartIn {
                pr: p.pr,
                head: p.head.clone(),
                parent: p.parent.clone(),
                receipt: PartReceipt {
                    run_id: p.receipt.run_id.clone(),
                    head: p.receipt.head.clone(),
                },
            });
        }
    }
    Ok(out)
}
