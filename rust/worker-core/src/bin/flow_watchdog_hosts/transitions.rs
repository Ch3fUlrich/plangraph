//! Durable host disk transitions. A disk event is queued before the `active` file records its key,
//! so a crash between the two writes leaves the event in the queue and the key out of `active`.
//! `reconcile` commits such an event's key first; `recovered` and `settle` retire the events of a
//! crossing the watchdog saw recover, so a later crossing is a new transition with a new id.
use super::{disks::Row, key::Key, names::Names};
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

/// An aggregate `host-disk` line from before filesystems were keyed becomes the keyed line of its
/// filesystem, but only when the host's row shows exactly one hot filesystem and nothing anonymous.
pub fn migrate(previous: &str, rows: &[Row]) -> String {
    join(
        previous
            .lines()
            .map(|line| {
                let Some(key) =
                    Key::parse(line).filter(|k| k.disk && k.fs.is_none() && !k.measured)
                else {
                    return line.to_owned();
                };
                let Some(row) = rows.iter().find(|row| row.host == key.host) else {
                    return line.to_owned();
                };
                let mut hot = row.groups.iter().filter(|(_, group)| group.hot());
                match (hot.next(), hot.next(), row.anonymous_hot) {
                    (Some((device, _)), None, false) => {
                        Key::disk(&key.host, Some(device), false).render()
                    }
                    _ => line.to_owned(),
                }
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

/// The durable id of a disk transition: its key and the time it was first queued.
pub fn transition(key: &str) -> Option<String> {
    Key::parse(key)
        .filter(|key| key.disk)
        .map(|key| format!("{}#{}", key.render(), super::local::now()))
}
