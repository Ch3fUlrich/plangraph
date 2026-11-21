//! A red train drops the failing part or is split by stacks (rule 3.52 (8), D-6535): every retry is a new
//! frozen candidate, cuts fall on stack boundaries, one stack is cut at a dependency-closed prefix, two halves
//! that pass alone land one after the other with a second full CI, and the train size follows the red rate.
use super::train::{stack_order, PartIn};
use std::collections::{BTreeMap, BTreeSet};

/// Parts of a train at the start, and its bounds (parts).
pub const START_SIZE: usize = 4;
pub const MIN_SIZE: usize = 1;
pub const MAX_SIZE: usize = 8;

/// The train size: four at the start, plus one after a green train, halved after a red one, within 1 and 8.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TrainSize(pub usize);

impl Default for TrainSize {
    fn default() -> Self {
        TrainSize(START_SIZE)
    }
}

impl TrainSize {
    pub fn after(self, green: bool) -> TrainSize {
        TrainSize(if green {
            (self.0 + 1).min(MAX_SIZE)
        } else {
            (self.0 / 2).max(MIN_SIZE)
        })
    }
}

/// Parts grouped by stack (a root and everything above it), in stack order.
pub fn stacks(parts: &[PartIn]) -> Vec<Vec<PartIn>> {
    let prs: BTreeSet<u64> = parts.iter().map(|p| p.pr).collect();
    let mut root_of: BTreeMap<u64, u64> = BTreeMap::new();
    let mut groups: Vec<(u64, Vec<PartIn>)> = Vec::new();
    for p in stack_order(parts) {
        let root = match p.parent.as_ref().filter(|q| prs.contains(&q.pr)) {
            Some(q) => root_of.get(&q.pr).copied().unwrap_or(q.pr),
            None => p.pr,
        };
        root_of.insert(p.pr, root);
        match groups.iter_mut().find(|(r, _)| *r == root) {
            Some((_, g)) => g.push(p),
            None => groups.push((root, vec![p])),
        }
    }
    groups.into_iter().map(|(_, g)| g).collect()
}

/// The part and every part above it.
pub fn with_descendants(parts: &[PartIn], pr: u64) -> Vec<PartIn> {
    let mut gone: BTreeSet<u64> = BTreeSet::from([pr]);
    let mut out = Vec::new();
    for p in stack_order(parts) {
        let above = p.parent.as_ref().is_some_and(|q| gone.contains(&q.pr));
        if p.pr == pr || above {
            gone.insert(p.pr);
            out.push(p);
        }
    }
    out
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Kind {
    /// The first frozen candidate of a retry (one part dropped, or the whole train).
    First,
    /// One of two halves cut at a stack boundary.
    Half,
    /// A dependency-closed prefix of the one failing stack.
    Prefix,
    /// The half that waited for its sibling: rebuilt against the new main, reviewed, full CI again.
    Second,
    /// The rest of a stack after its prefix landed and was verified in main.
    Remainder,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Candidate {
    pub id: String,
    pub parts: Vec<PartIn>,
    pub kind: Kind,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Owner {
    /// The part's own writer session, with the job log.
    Writer,
    /// A part evicted twice goes to routing.
    Routing,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Action {
    /// Freeze this candidate and run full CI on it.
    Freeze(Candidate),
    /// Land this candidate now (it passed full CI).
    Land(Candidate),
    /// Rebuild this candidate against the new main, review it, run full CI again.
    Rebuild(Candidate),
    /// A part leaves the train and goes back with the job log.
    Evict {
        pr: u64,
        head: String,
        log: String,
        to: Owner,
    },
    /// Parts that wait for the part or prefix below them; they are not blamed.
    Blocked(Vec<u64>),
    /// The second run was red: the part stays unmerged.
    Unmerged(Vec<PartIn>),
}

/// The state of one red-train recovery.
#[derive(Default)]
pub struct Session {
    next: u32,
    cands: BTreeMap<String, Candidate>,
    /// Sibling pairs of halves, by candidate id (a half can be split again into its own pair).
    pairs: Vec<(String, String)>,
    green: BTreeSet<String>,
    /// The second half of a pair whose first half was released, by the first half's id.
    held: BTreeMap<String, Candidate>,
    /// Candidates already released for landing: each lands at most once.
    landed: BTreeSet<String>,
    deferred: Vec<PartIn>,
    strikes: BTreeMap<u64, u32>,
}

impl Session {
    fn make(&mut self, parts: Vec<PartIn>, kind: Kind) -> Candidate {
        self.next += 1;
        let c = Candidate {
            id: format!("c{}", self.next),
            parts,
            kind,
        };
        self.cands.insert(c.id.clone(), c.clone());
        c
    }

    /// Starts with the train that went red.
    pub fn new(train: Vec<PartIn>) -> (Session, Candidate) {
        let mut s = Session::default();
        let c = s.make(train, Kind::First);
        (s, c)
    }

    /// As `new`, with the evictions earlier trains already gave each part (pr -> count).
    pub fn with_strikes(train: Vec<PartIn>, strikes: BTreeMap<u64, u32>) -> (Session, Candidate) {
        let (mut s, c) = Session::new(train);
        s.strikes = strikes;
        (s, c)
    }

    /// Evictions per part so far, to carry into the next train.
    pub fn strikes(&self) -> &BTreeMap<u64, u32> {
        &self.strikes
    }

    fn evict(&mut self, p: &PartIn, log: &str) -> Action {
        let n = self.strikes.entry(p.pr).or_insert(0);
        *n += 1;
        Action::Evict {
            pr: p.pr,
            head: p.head.clone(),
            log: log.to_owned(),
            to: if *n >= 2 {
                Owner::Routing
            } else {
                Owner::Writer
            },
        }
    }

    /// Parts waiting for something below them; they ride the next train.
    pub fn deferred(&self) -> &[PartIn] {
        &self.deferred
    }

    /// Full CI on candidate `id` is red. `hints` are the parts the failing test paths point at, most likely first.
    pub fn red(&mut self, id: &str, hints: &[u64], log: &str) -> Vec<Action> {
        let Some(cand) = self.cands.remove(id) else {
            return vec![];
        };
        if cand.kind == Kind::Second {
            return vec![Action::Unmerged(cand.parts)];
        }
        let mut released = Vec::new();
        if cand.kind == Kind::Half {
            // A sibling that passed alone while this half was running lands now; this half goes on alone.
            if let Some(i) = self.pairs.iter().position(|(a, b)| a == id || b == id) {
                let (a, b) = self.pairs.remove(i);
                let sibling = if id == a { b } else { a };
                if self.green.contains(&sibling) {
                    released.extend(self.land_once(&sibling));
                }
            }
        }
        let mut out = released;
        out.extend(self.red_rest(cand, hints, log));
        out
    }

    fn land_once(&mut self, id: &str) -> Option<Action> {
        let cand = self.cands.get(id)?.clone();
        self.landed
            .insert(id.to_owned())
            .then_some(Action::Land(cand))
    }

    fn red_rest(&mut self, cand: Candidate, hints: &[u64], log: &str) -> Vec<Action> {
        if let Some(p) = hints
            .iter()
            .find_map(|h| cand.parts.iter().find(|p| p.pr == *h))
        {
            let drop = with_descendants(&cand.parts, p.pr);
            let mut out = vec![self.evict(p, log)];
            let blocked: Vec<PartIn> = drop.iter().filter(|d| d.pr != p.pr).cloned().collect();
            if !blocked.is_empty() {
                out.push(Action::Blocked(blocked.iter().map(|b| b.pr).collect()));
                self.deferred.extend(blocked);
            }
            let dropped: BTreeSet<u64> = drop.iter().map(|d| d.pr).collect();
            let rest: Vec<PartIn> = cand
                .parts
                .iter()
                .filter(|q| !dropped.contains(&q.pr))
                .cloned()
                .collect();
            if !rest.is_empty() {
                let c = self.make(rest, Kind::First);
                out.push(Action::Freeze(c));
            }
            return out;
        }
        let groups = stacks(&cand.parts);
        if groups.len() >= 2 {
            let total = cand.parts.len();
            // Cut at the stack boundary that balances the two halves best (halves may be uneven).
            let (mut best, mut acc, mut cut) = (usize::MAX, 0, 1);
            for (i, g) in groups.iter().enumerate().take(groups.len() - 1) {
                acc += g.len();
                let diff = acc.abs_diff(total - acc);
                if diff < best {
                    best = diff;
                    cut = i + 1;
                }
            }
            let a: Vec<PartIn> = groups[..cut].iter().flatten().cloned().collect();
            let b: Vec<PartIn> = groups[cut..].iter().flatten().cloned().collect();
            let (ca, cb) = (self.make(a, Kind::Half), self.make(b, Kind::Half));
            self.pairs.push((ca.id.clone(), cb.id.clone()));
            return vec![Action::Freeze(ca), Action::Freeze(cb)];
        }
        let chain = &groups[0];
        if chain.len() == 1 {
            return vec![self.evict(&chain[0], log)];
        }
        let keep = chain.len() / 2;
        let prefix = chain[..keep].to_vec();
        let rest = chain[keep..].to_vec();
        let blocked = Action::Blocked(rest.iter().map(|p| p.pr).collect());
        self.deferred.extend(rest);
        let c = self.make(prefix, Kind::Prefix);
        vec![blocked, Action::Freeze(c)]
    }

    /// Full CI on candidate `id` is green. A repeated notification changes nothing.
    pub fn green(&mut self, id: &str) -> Vec<Action> {
        let Some(cand) = self.cands.get(id).cloned() else {
            return vec![];
        };
        if cand.kind != Kind::Half {
            return self.land_once(id).into_iter().collect();
        }
        if !self.green.insert(id.to_owned()) {
            return vec![];
        }
        let Some(i) = self.pairs.iter().position(|(a, b)| a == id || b == id) else {
            // The sibling went red first and the pair was released: this half lands alone.
            return self.land_once(id).into_iter().collect();
        };
        let (a, b) = self.pairs[i].clone();
        let sibling = if id == a { &b } else { &a };
        if self.green.contains(sibling) {
            // Both passed against the old base: the first lands, the other is rebuilt after it.
            self.pairs.remove(i);
            if let Some(second) = self.cands.get(&b).cloned() {
                self.held.insert(a.clone(), second);
            }
            return self.land_once(&a).into_iter().collect();
        }
        if !self.cands.contains_key(sibling) {
            // The sibling was handled as red already (it is gone): this half lands alone.
            return self.land_once(id).into_iter().collect();
        }
        vec![]
    }

    /// The landed candidate's exact head is verified in main (or not). A prefix then retargets its
    /// remainder; the held second half is rebuilt against the new main.
    pub fn landed(&mut self, id: &str, verified: bool) -> Vec<Action> {
        if !verified {
            return vec![];
        }
        let Some(cand) = self.cands.get(id).cloned() else {
            return vec![];
        };
        if let Some(second) = self.held.remove(id) {
            let c = self.make(second.parts, Kind::Second);
            self.cands.remove(&second.id);
            return vec![Action::Rebuild(c)];
        }
        if cand.kind == Kind::Prefix && !self.deferred.is_empty() {
            let rest = std::mem::take(&mut self.deferred);
            let c = self.make(rest, Kind::Remainder);
            return vec![Action::Rebuild(c)];
        }
        vec![]
    }
}
