//! Durable host disk transitions. A disk event is queued before the `active` file records its key,
//! so a crash between the two writes leaves the event in the queue and the key out of `active`.
//! `reconcile` commits such an event's key first; `recovered` and `settle` retire the events of a
//! crossing the watchdog saw recover, so a later crossing is a new transition with a new id.
use super::{key::Key, names::Names};
use serde_json::Value;
use std::collections::BTreeSet;

fn join(lines: Vec<String>) -> String {
    let mut seen = BTreeSet::new();
    lines
        .into_iter()
        .filter(|line| seen.insert(line.clone()))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Rewrite the host of every host alert line to the registry's canonical id.
pub fn canonical(previous: &str, names: &Names) -> String {
    join(
        previous
            .lines()
            .map(|line| match Key::parse(line) {
                Some(mut key) => {
                    key.host = names.canonical(&key.host);
                    key.render()
                }
                None => line.to_owned(),
            })
            .collect(),
    )
}

fn live_event_key(event: &Value) -> Option<Key> {
    (event["transition"].is_string() && event["recovered"] != true)
        .then(|| Key::parse(event["active"].as_str()?))?
}

/// Add the key of every queued, unretired disk event that `previous` does not hold yet.
pub fn reconcile(previous: &str, queue: &[Value]) -> String {
    let mut lines: Vec<String> = previous.lines().map(str::to_owned).collect();
    for key in queue.iter().filter_map(live_event_key) {
        let line = key.render();
        if lines.contains(&line) {
            continue;
        }
        let same = |l: &String| Key::parse(l).is_some_and(|k| k.crossing() == key.crossing());
        if key.measured {
            lines.retain(|l| !same(l));
        } else if lines.iter().any(same) {
            continue;
        }
        lines.push(line);
    }
    join(lines)
}

/// Crossings that `previous` held and `active` no longer holds in any form: observed recoveries.
pub fn recovered(previous: &str, active: &str) -> Vec<Key> {
    let now: BTreeSet<_> = active
        .lines()
        .filter_map(Key::parse)
        .map(|key| key.crossing())
        .collect();
    let gone: BTreeSet<_> = previous
        .lines()
        .filter_map(Key::parse)
        .filter(|key| key.disk)
        .map(|key| key.crossing())
        .filter(|key| !now.contains(key))
        .collect();
    gone.into_iter().collect()
}

/// Retire the queued events of recovered crossings; true when the queue changed.
pub fn settle(queue: &mut [Value], crossings: &[Key]) -> bool {
    let mut changed = false;
    for event in queue.iter_mut() {
        let Some(key) = live_event_key(event) else {
            continue;
        };
        if crossings.contains(&key.crossing()) {
            event["recovered"] = true.into();
            changed = true;
        }
    }
    changed
}

/// A disk key that already has an unretired queued event is not queued again.
pub fn queued(queue: &[Value], key: &str) -> bool {
    queue
        .iter()
        .filter_map(live_event_key)
        .any(|queued| queued.render() == key)
}

/// Whether a queued event and its pending copy are the same event. Disk events are matched by
/// their transition id: recovery marks the queued copy `recovered`, so the two no longer compare
/// equal as whole values.
pub fn same_event(queued: &Value, pending: &Value) -> bool {
    queued == pending
        || (queued["transition"].is_string() && queued["transition"] == pending["transition"])
}

pub fn is_disk_key(key: &str) -> bool {
    Key::parse(key).is_some_and(|key| key.disk)
}

/// A new transition id: the key and the next number of a counter kept in the state directory, so
/// two crossings never share an id, whatever the clock says. A crash after the counter moved
/// leaves a gap, never a repeat.
pub fn next_transition(state: &std::path::Path, key: &str) -> std::io::Result<String> {
    let path = state.join("disk-transition-seq");
    let last: u64 = std::fs::read_to_string(&path)
        .ok()
        .and_then(|text| text.trim().parse().ok())
        .unwrap_or(0);
    let next = last + 1;
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, next.to_string())?;
    std::fs::rename(tmp, path)?;
    Ok(format!("{key}#{next}"))
}
