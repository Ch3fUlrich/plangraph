//! Shared snapshot check for digest, heartbeat and scheduler consumers.
//! No collection, slot grants, keeper mutation or inferred liveness.
use crate::{
    disk_hygiene::Thresholds,
    hosts::Caps,
    slots::{writer_capacity, SlotTable},
};
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub hosts: Vec<Host>,
    pub ready_items: Vec<ReadyItem>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Host {
    pub host: String,
    pub caps: Caps,
    pub table: SlotTable,
    pub rss_mb: BTreeMap<String, u64>,
    pub mem_available_mb: u64,
    pub writer_mb: u64,
    pub disk_free_bytes: u64,
    pub cpus: u64,
    pub load1: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReadyItem {
    pub key: String,
    pub ready: bool,
    pub blocked: bool,
    pub claimed: bool,
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "-_.:".contains(c))
}

impl Snapshot {
    /// One line per eligible host, sorted by host identifier.
    /// Bad samples are errors, never fabricated free capacity.
    pub fn messages(&self) -> Result<Vec<String>, &'static str> {
        let mut names = BTreeSet::new();
        for host in &self.hosts {
            if !identifier(&host.host)
                || !names.insert(&host.host)
                || host.table.version != 1
                || host.writer_mb == 0
                || host.cpus == 0
                || !host.load1.is_finite()
                || host.load1 < 0.0
            {
                return Err("invalid host idle-capacity sample");
            }
        }
        let mut ready = BTreeMap::new();
        for item in &self.ready_items {
            if !identifier(&item.key) {
                return Err("invalid ready item key");
            }
            let eligible = item.ready && !item.blocked && !item.claimed;
            // Conflicting duplicates cannot manufacture ready work.
            ready
                .entry(&item.key)
                .and_modify(|old| *old &= eligible)
                .or_insert(eligible);
        }
        let ready = ready.values().filter(|&&eligible| eligible).count();
        if ready == 0 {
            return Ok(Vec::new());
        }
        let mut messages = Vec::new();
        for host in &self.hosts {
            if host.disk_free_bytes < Thresholds::default().alarm_free_bytes
                || host.load1 >= host.cpus as f64
            {
                continue;
            }
            let slots = writer_capacity(
                &host.table.active,
                &host.caps,
                host.mem_available_mb,
                |slot| host.rss_mb.get(&slot.run_id).copied().unwrap_or(0),
                host.writer_mb,
            );
            if slots > 0 {
                messages.push(format!("IDLE {} slots={slots} ready={ready}", host.host));
            }
        }
        messages.sort();
        Ok(messages)
    }
}
