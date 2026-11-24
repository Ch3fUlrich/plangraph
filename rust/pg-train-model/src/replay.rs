//! Replay of recorded batches through a selection rule on one timeline, one train at a time.
//! The objective is parts merged per hour (rule 3.74 (1)); conflicts are parts that overlap an earlier part of
//! their train (a path-level stand-in for merge conflicts); the red rate is unknown where no CI result is recorded.
use super::rules::{choose, Left, Pool, Rule};
use serde::Deserialize;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Deserialize)]
pub struct Batch {
    pub id: String,
    pub train: u8,
    /// Epoch seconds the batch was ready (committer time of its head).
    pub ready: i64,
    pub commits: u32,
    pub paths: Vec<u32>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct History {
    pub version: u8,
    pub source: String,
    /// Epoch seconds each recorded train landed.
    pub landed: BTreeMap<String, i64>,
    /// Distinct paths touched by any batch; a batch's paths are indices below this.
    pub path_count: usize,
    pub batches: Vec<Batch>,
}

/// What a train costs; assumptions, applied alike to every rule compared.
#[derive(Clone, Copy, Debug)]
pub struct Costs {
    pub base_secs: i64,
    pub per_part_secs: i64,
    pub per_conflict_secs: i64,
}

#[derive(Clone, Copy, Debug)]
pub struct Config {
    pub rule: Rule,
    /// An ordinary train is cut when the oldest ready part has waited this long, or at the size target.
    pub cut_after_secs: i64,
    pub costs: Costs,
    /// The next candidate is cut as soon as the previous train starts, and tested meanwhile; trains
    /// still land one after the other, each on the previous one (tested fast-forward).
    pub speculative: bool,
}

impl Config {
    /// Rule 3.74 (1): every ready part rides, a train starts at once.
    pub fn every_ready_rides(costs: Costs) -> Config {
        Config {
            rule: Rule::new(usize::MAX, 0, super::Order::Oldest),
            cut_after_secs: 0,
            costs,
            speculative: false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Cut {
    pub start: i64,
    pub land: i64,
    pub parts: Vec<String>,
    pub conflicts: usize,
}

#[derive(Clone, Debug)]
pub struct Report {
    pub trains: Vec<Cut>,
    pub conflicts: usize,
    pub longest_wait_secs: i64,
    pub mean_wait_secs: i64,
    pub makespan_secs: i64,
    pub parts_per_hour: f64,
    /// `None` is "unknown": the history records no CI result per train.
    pub red_rate: Option<f64>,
}

struct Sim<'a> {
    batches: &'a [Batch],
    now: i64,
    deferrals: Vec<u32>,
}

impl Pool for Sim<'_> {
    fn waited(&self, p: usize) -> u32 {
        (self.now - self.batches[p].ready).clamp(0, u32::MAX as i64) as u32
    }
    fn deferrals(&self, p: usize) -> u32 {
        self.deferrals[p]
    }
    fn overlaps(&self, a: usize, b: usize) -> bool {
        let (a, b) = (&self.batches[a].paths, &self.batches[b].paths);
        a.iter().any(|p| b.binary_search(p).is_ok())
    }
    fn parent(&self, _: usize) -> Option<usize> {
        None
    }
    fn verified(&self, _: usize) -> bool {
        false
    }
}

pub fn replay(h: &History, c: &Config) -> Report {
    let mut order: Vec<usize> = (0..h.batches.len()).collect();
    order.sort_by_key(|&i| h.batches[i].ready);
    let mut landed = vec![false; h.batches.len()];
    let mut sim = Sim {
        batches: &h.batches,
        now: 0,
        deferrals: vec![0; h.batches.len()],
    };
    let mut trains: Vec<Cut> = Vec::new();
    let mut waits: Vec<i64> = Vec::new();
    let mut now = order.first().map_or(0, |&i| h.batches[i].ready);
    while landed.iter().any(|l| !l) {
        let ready: Vec<usize> = order
            .iter()
            .copied()
            .filter(|&i| !landed[i] && h.batches[i].ready <= now)
            .collect();
        let oldest = ready.iter().map(|&i| h.batches[i].ready).min();
        let due = !ready.is_empty()
            && (ready.len() >= c.rule.target
                || oldest.is_some_and(|o| now - o >= c.cut_after_secs));
        if !due {
            let next_arrival = order
                .iter()
                .map(|&i| h.batches[i].ready)
                .filter(|&r| r > now)
                .min();
            let timer = oldest.map(|o| o + c.cut_after_secs).filter(|&t| t > now);
            now = [next_arrival, timer]
                .into_iter()
                .flatten()
                .min()
                .unwrap_or(now + 1);
            continue;
        }
        sim.now = now;
        let (riding, left) = choose(&sim, &ready, c.rule);
        for (p, l) in left {
            if matches!(l, Left::Overlap { .. } | Left::Target) {
                sim.deferrals[p] += 1;
            }
        }
        // A part that overlaps one riding before it needs one resolution.
        let conflicts = riding
            .iter()
            .enumerate()
            .filter(|&(k, &b)| riding[..k].iter().any(|&a| sim.overlaps(a, b)))
            .count();
        let cost = &c.costs;
        let tested = now
            + cost.base_secs
            + cost.per_part_secs * (riding.len() as i64 - 1)
            + cost.per_conflict_secs * conflicts as i64;
        // Main moves to one train at a time: a candidate lands after its predecessor and its own CI.
        let before = trains.last().map(|t| t.land);
        let land = tested.max(before.unwrap_or(0));
        for &i in &riding {
            landed[i] = true;
            waits.push(land - h.batches[i].ready);
        }
        trains.push(Cut {
            start: now,
            land,
            parts: riding.iter().map(|&i| h.batches[i].id.clone()).collect(),
            conflicts,
        });
        // A candidate waits only for the train two back to land: one is on main's tip, one under test.
        now = if c.speculative {
            now.max(trains.iter().rev().nth(1).map_or(now, |t| t.land))
        } else {
            land
        };
    }
    let first = h.batches.iter().map(|b| b.ready).min().unwrap_or(0);
    let makespan = trains.last().map_or(0, |t| t.land - first);
    Report {
        conflicts: trains.iter().map(|t| t.conflicts).sum(),
        longest_wait_secs: waits.iter().copied().max().unwrap_or(0),
        mean_wait_secs: waits.iter().sum::<i64>() / waits.len().max(1) as i64,
        makespan_secs: makespan,
        parts_per_hour: waits.len() as f64 * 3600.0 / makespan.max(1) as f64,
        red_rate: None,
        trains,
    }
}
