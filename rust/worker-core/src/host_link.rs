//! Host-local outage state, publishing to the existing watchdog outbox.
use crate::run::record_io::atomic_write_private;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs, io,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

pub fn probe_peer(peer: &str) -> io::Result<bool> {
    if peer.is_empty()
        || peer.starts_with('-')
        || !peer
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte))
    {
        return Err(io::Error::other("invalid Tailscale peer"));
    }
    match crate::host_command::capture(
        Command::new("tailscale").args([
            "ping",
            "--c=1",
            "--timeout=5s",
            "--until-direct=false",
            peer,
        ]),
        Duration::from_secs(7),
    ) {
        Ok(output) => Ok(output.status.success()),
        Err(error) if error.kind() == io::ErrorKind::TimedOut => Ok(false),
        Err(error) => Err(error),
    }
}

pub fn probe_ssh(destination: &str) -> io::Result<bool> {
    use crate::host_command::{capture, ssh};
    match capture(ssh(destination)?.arg("exit 0"), Duration::from_secs(7)) {
        Ok(result) => result
            .status
            .code()
            .map(|code| code != 255)
            .ok_or_else(|| io::Error::other("SSH probe was interrupted")),
        Err(error) if error.kind() == io::ErrorKind::TimedOut => Ok(false),
        Err(error) => Err(error),
    }
}

fn write_outbox(root: &Path, filename: &str, event: &Value) -> io::Result<()> {
    atomic_write_private(
        &root.join("watchdog/outbox").join(filename),
        &serde_json::to_vec(event)?,
    )
}

pub(crate) fn enqueue_worker_alert(root: &Path, event: &Value) -> io::Result<()> {
    let host = event["host"]
        .as_str()
        .filter(|host| crate::fleet_series::valid_host(host))
        .ok_or_else(|| io::Error::other("invalid worker alert host"))?;
    let now = event["observed_at"]
        .as_u64()
        .ok_or_else(|| io::Error::other("invalid worker alert time"))?;
    let sequence = event["sequence"]
        .as_u64()
        .ok_or_else(|| io::Error::other("invalid worker alert sequence"))?;
    let id = format!("{:x}", Sha256::digest(serde_json::to_vec(event)?));
    let mut event = event.clone();
    event["event_id"] = json!(id);
    write_outbox(
        root,
        &format!("{now:020}-worker-host-{host}-{sequence:020}-{id}.json"),
        &event,
    )
}

pub fn worker_alert_filename(name: &str) -> bool {
    name.as_bytes()
        .get(..20)
        .is_some_and(|head| head.iter().all(u8::is_ascii_digit))
        && name
            .get(20..)
            .is_some_and(|tail| tail.starts_with("-worker-host-") && tail.ends_with(".json"))
}

#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct State {
    down_since: Option<u64>,
    alerted: bool,
    last_observed: u64,
}

struct Link<'a> {
    root: &'a Path,
    key: String,
    state: State,
}

impl<'a> Link<'a> {
    fn read(root: &'a Path, identity: &impl Serialize) -> io::Result<Self> {
        let key = format!("{:x}", Sha256::digest(serde_json::to_vec(identity)?));
        let mut link = Self {
            root,
            key,
            state: State::default(),
        };
        match fs::read(link.path()) {
            Ok(bytes) => link.state = serde_json::from_slice(&bytes)?,
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            Err(e) => return Err(e),
        }
        if link
            .state
            .down_since
            .is_some_and(|since| since > link.state.last_observed)
            || (link.state.alerted && link.state.down_since.is_none())
        {
            return Err(io::Error::other("invalid host-link state"));
        }
        Ok(link)
    }

    fn path(&self) -> PathBuf {
        self.root
            .join("watchdog/host-link")
            .join(format!("{}.json", self.key))
    }

    fn enqueue(&self, rule: &str, since: u64, mut event: Value) -> io::Result<()> {
        let namespace = if rule == "host-link-down" {
            "host-link"
        } else {
            rule
        };
        let event_id = format!(
            "{:x}",
            Sha256::digest(format!("{namespace}:{}:{since}", self.key))
        );
        event["kind"] = json!("alert");
        event["recipient"] = json!("routing");
        event["rule"] = json!(rule);
        event["down_since"] = json!(since);
        event["observed_at"] = json!(self.state.last_observed);
        event["event_id"] = json!(event_id);
        write_outbox(
            self.root,
            &format!("{since:020}-{rule}-{event_id}.json"),
            &event,
        )
    }

    fn observe(
        &mut self,
        now: u64,
        reachable: bool,
        due: impl FnOnce(u64) -> bool,
        publish: impl FnOnce(&Self, u64, bool) -> io::Result<()>,
        notify_recovery: bool,
    ) -> io::Result<Option<u64>> {
        if now < self.state.last_observed {
            return Err(io::Error::other("host-link clock moved backwards"));
        }
        self.state.last_observed = now;
        let mut transition = None;
        if reachable {
            if let Some(since) = self
                .state
                .down_since
                .filter(|_| self.state.alerted && notify_recovery)
            {
                publish(self, since, true)?;
                transition = Some(since);
            }
            self.state.down_since = None;
            self.state.alerted = false;
        } else {
            let previous = self.state.down_since;
            let since = *self.state.down_since.get_or_insert(now);
            if previous.is_some() && !self.state.alerted && due(since) {
                publish(self, since, false)?;
                self.state.alerted = true;
                transition = Some(since);
            }
        }
        atomic_write_private(&self.path(), &serde_json::to_vec(&self.state)?)?;
        Ok(transition)
    }
}

/// Observe one completed probe. Call serially per host/peer, at least once a minute.
/// A failed enqueue never acknowledges an alert; subsequent ticks try delivery again.
/// Returns whether this tick enqueued an alert. Existing pending alerts survive recovery.
pub fn observe(root: &Path, host: &str, peer: &str, now: u64, reachable: bool) -> io::Result<bool> {
    if host.is_empty() || peer.is_empty() {
        return Err(io::Error::other("host and peer required"));
    }
    let mut link = Link::read(root, &(host, peer))?;
    link.observe(
        now,
        reachable,
        |since| now - since > 300,
        |link, since, _| {
            link.enqueue(
                "host-link-down",
                since,
                json!({
                    "host": host, "peer": peer,
                    "item_key": "plangraph:LE-BUILD-HOSTS-UNREACHABLE-OVER-TAILSCALE"
                }),
            )
        },
        false,
    )
    .map(|transition| transition.is_some())
}

fn ssh_link<'a>(root: &'a Path, destination: &str) -> io::Result<Link<'a>> {
    Link::read(root, &json!({"ssh": destination}))
}

/// Record a coordinator-side probe, with diagnostics collected on transition only.
pub fn observe_ssh(
    root: &Path,
    destination: &str,
    now: u64,
    reachable: bool,
    evidence: impl FnOnce() -> Value,
) -> io::Result<Option<u64>> {
    ssh_link(root, destination)?.observe(
        now,
        reachable,
        |_| true,
        |link, since, recovered| {
            let mut event = evidence();
            event["destination"] = json!(destination);
            event["item_key"] = json!("plangraph:LE-CODING-VM-LOSES-WORKER-HOSTS-ON-IP-CHANGE");
            link.enqueue(
                if recovered {
                    "host-ssh-recovered"
                } else {
                    "host-ssh-down"
                },
                since,
                event,
            )
        },
        true,
    )
}

/// Return the first failure time of a confirmed outage on a configured SSH route.
pub fn ssh_down_since(root: &Path, destination: &str) -> io::Result<Option<u64>> {
    let state = ssh_link(root, destination)?.state;
    Ok(state.down_since.filter(|_| state.alerted))
}
