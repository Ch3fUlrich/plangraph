//! Host-local outage state, publishing to the existing watchdog outbox.
use crate::run::record_io::atomic_write_private;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{fs, io, path::Path};

#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct State {
    down_since: Option<u64>,
    alerted: bool,
    last_observed: u64,
}

fn write_private(path: &Path, value: &impl Serialize) -> io::Result<()> {
    atomic_write_private(path, &serde_json::to_vec(value)?)
}

/// Observe one completed probe. Call serially per host/peer, at least once a minute.
/// A failed enqueue never acknowledges an alert; subsequent ticks try delivery again.
/// Returns whether this tick enqueued an alert. Existing pending alerts survive recovery.
pub fn observe(root: &Path, host: &str, peer: &str, now: u64, reachable: bool) -> io::Result<bool> {
    if host.is_empty() || peer.is_empty() {
        return Err(io::Error::other("host and peer required"));
    }
    let identity = serde_json::to_vec(&(host, peer))?;
    let key = format!("{:x}", Sha256::digest(identity));
    let path = root.join("watchdog/host-link").join(format!("{key}.json"));
    let mut state: State = match fs::read(&path) {
        Ok(bytes) => serde_json::from_slice(&bytes)?,
        Err(e) if e.kind() == io::ErrorKind::NotFound => State::default(),
        Err(e) => return Err(e),
    };
    if now < state.last_observed
        || state
            .down_since
            .is_some_and(|since| since > state.last_observed)
        || (state.alerted && state.down_since.is_none())
    {
        return Err(io::Error::other(
            "invalid host-link state or clock moved backwards",
        ));
    }
    state.last_observed = now;
    let mut queued = false;
    if reachable {
        state.down_since = None;
        state.alerted = false;
    } else {
        let since = *state.down_since.get_or_insert(now);
        if now - since > 300 && !state.alerted {
            let event_id = format!("{:x}", Sha256::digest(format!("host-link:{key}:{since}")));
            let event = serde_json::json!({
                "kind": "alert", "recipient": "routing", "rule": "host-link-down",
                "host": host, "peer": peer, "down_since": since, "observed_at": now,
                "event_id": event_id,
                "item_key": "plangraph:LE-BUILD-HOSTS-UNREACHABLE-OVER-TAILSCALE"
            });
            write_private(
                &root
                    .join("watchdog/outbox")
                    .join(format!("{event_id}.json")),
                &event,
            )?;
            state.alerted = true;
            queued = true;
        }
    }
    write_private(&path, &state)?;
    Ok(queued)
}
