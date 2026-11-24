//! The one selection rule, shared by the transition table and the replay: which ready parts ride and why
//! each other part is left out. `bound` 0 and an unbounded `target` make every ready part ride.
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Order {
    /// Take the largest disjoint set first: parts with the fewest overlaps go first.
    Count,
    /// Take the oldest part first; a younger overlapping part is deferred.
    Oldest,
}

/// What the rule reads about the ready parts.
pub trait Pool {
    fn waited(&self, part: usize) -> u32;
    fn overlaps(&self, a: usize, b: usize) -> bool;
    fn parent(&self, part: usize) -> Option<usize>;
    /// The part has a verified landing.
    fn verified(&self, part: usize) -> bool;
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Rule {
    pub target: usize,
    /// An overlapping part is deferred while it has waited less than this; then it rides.
    pub bound: u32,
    pub order: Order,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Left {
    Overlap { with: usize, waited: u32 },
    ParentNotLanded { parent: usize },
    Target,
}

/// Candidates in rule order, every parent before its children.
fn candidates(pool: &dyn Pool, ready: &[usize], rule: Rule) -> Vec<usize> {
    let forced = |p: usize| pool.waited(p) >= rule.bound;
    let degree = |p: usize| {
        ready
            .iter()
            .filter(|&&q| q != p && pool.overlaps(p, q))
            .count()
    };
    let mut sorted = ready.to_vec();
    match rule.order {
        Order::Oldest => sorted.sort_by_key(|&p| std::cmp::Reverse(pool.waited(p))),
        Order::Count => {
            sorted.sort_by_key(|&p| (!forced(p), if forced(p) { 0 } else { degree(p) }))
        }
    }
    fn push(pool: &dyn Pool, ready: &[usize], p: usize, out: &mut Vec<usize>) {
        if out.contains(&p) {
            return;
        }
        if let Some(q) = pool.parent(p).filter(|q| ready.contains(q)) {
            push(pool, ready, q, out);
        }
        out.push(p);
    }
    let mut out = Vec::new();
    for p in sorted {
        push(pool, ready, p, &mut out);
    }
    out
}

/// The parts that ride, in order, and every other ready part with its disposition.
pub fn choose(pool: &dyn Pool, ready: &[usize], rule: Rule) -> (Vec<usize>, Vec<(usize, Left)>) {
    let (mut riding, mut left) = (Vec::new(), Vec::new());
    for p in candidates(pool, ready, rule) {
        let waited = pool.waited(p);
        let missing_parent = pool
            .parent(p)
            .filter(|q| !riding.contains(q) && !pool.verified(*q));
        if riding.len() >= rule.target {
            left.push((p, Left::Target));
        } else if let Some(parent) = missing_parent {
            left.push((p, Left::ParentNotLanded { parent }));
        } else if let Some(&with) = riding
            .iter()
            .find(|&&q| pool.overlaps(p, q))
            .filter(|_| waited < rule.bound)
        {
            left.push((p, Left::Overlap { with, waited }));
        } else {
            riding.push(p);
        }
    }
    (riding, left)
}
