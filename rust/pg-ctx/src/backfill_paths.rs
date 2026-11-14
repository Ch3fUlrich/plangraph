//! Proposed D-5572(4) remediation paths; these do not grant verdict authority.
pub struct Path {
    pub class: &'static str,
    pub slug: &'static str,
    pub path: &'static str,
    pub rules: &'static str,
    pub owner: &'static str,
}

// First match is the primary class. Missing records from another worker never
// override specific evidence found by a worker with records.
pub const PATHS: &[Path] = &[
    Path { class: "item key mismatch", slug: "KEY-MISMATCH",
        path: "Routing names exact key and PR in the close list; use close-merged after identity correction, without inventing a seat or a hand verdict.",
        rules: "D-5572(4); D-3551; INREVIEW-CLOSE-ROUTER-PATH", owner: "routing (identity/close list); project L1 (evidence)" },
    Path { class: "PR number mismatch", slug: "PR-MISMATCH",
        path: "Routing names exact key and PR in the close list; resolve binding through close-merged, no new seat solely for a PR-label mismatch.",
        rules: "D-5572(4); D-3551; INREVIEW-CLOSE-ROUTER-PATH", owner: "routing (PR binding); project L1 (evidence)" },
    Path { class: "no commit evidence", slug: "NO-COMMIT",
        path: "Find merged Plan-Item commit or PR, attach commit+ci; if nothing merged, route release to open or guarded cancellation, never post a verdict.",
        rules: "D-5572(4); D-3551; KEEPER-HYGIENE", owner: "project L1 (commit/CI); routing (release/cancel disposition)" },
    Path { class: "unknown writer", slug: "UNKNOWN-WRITER",
        path: "Resolve the union of Writer-trailer and lane-run writer families; unresolved keys need seats outside every candidate family, or an explicit operator waiver list.",
        rules: "D-5572(4); D-5092; D-5553; D-3551", owner: "project L1 (writer witnesses/seats); routing (waiver list)" },
    Path { class: "head mismatch", slug: "HEAD-MISMATCH",
        path: "Propose merge-only delta acceptance only with tree-diff proof and a routing ruling through close-merged; otherwise spawn a delta seat. Name ambiguous commits per key in the router close list. Keep seat_verdict exact-head checks.",
        rules: "D-5572(4) proposal requires routing confirmation; D-3551; INREVIEW-CLOSE-ROUTER-PATH", owner: "project L1 (tree/delta proof); routing (ambiguity/close list)" },
    Path { class: "pinned seat", slug: "PINNED-SEAT",
        path: "No counted seat exists: spawn one unpinned route seat per missing family on the merged head so a complete spawner record exists; post only through seat_verdict or close-merged.",
        rules: "D-5092; D-5553; D-3551", owner: "project L1 (spawner seats); routing (close list)" },
    Path { class: "served_model unwitnessed", slug: "UNWITNESSED",
        path: "No counted seat exists: spawn one unpinned route seat per missing family on the merged head with witnessed model and complete spawner record.",
        rules: "D-5092; D-5553; D-3551", owner: "project L1 (witnessed seats); routing (close list)" },
    Path { class: "no record", slug: "NO-RECORD",
        path: "No counted seat exists: spawn one unpinned route seat per missing family on the merged head; require the spawner record, then seat_verdict or close-merged.",
        rules: "D-5092; D-5553; D-3551", owner: "project L1 (spawner records/seats); routing (close list)" },
];

pub fn classify(reason: &str) -> Option<usize> {
    let reason = reason.split(" [seat=").next().unwrap_or(reason);
    PATHS.iter().position(|p| {
        reason.starts_with(p.class)
            || p.class == "served_model unwitnessed" && reason.contains("unwitnessed")
    })
}
