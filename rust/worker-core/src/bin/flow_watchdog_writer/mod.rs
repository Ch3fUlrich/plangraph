use super::Alert;
use serde::Deserialize;
use serde_json::json;
use std::collections::BTreeMap;

#[derive(Deserialize)]
pub(super) struct Activity {
    now: u64,
    writers: Vec<Writer>,
}

#[derive(Deserialize)]
struct Writer {
    host: String,
    session_id: String,
    parent: String,
    busy: bool,
    idle_since: Option<u64>,
    head: String,
    last_commit: u64,
    review: Option<Review>,
    status_path: String,
    status: Option<Status>,
}

#[derive(Deserialize)]
struct Review {
    head: String,
    verdict: String,
}

#[derive(Deserialize)]
struct Status {
    path: String,
    last_line: String,
}

impl Status {
    fn is_report(&self) -> bool {
        let mut words = self.last_line.split_whitespace();
        match words.next() {
            Some("READY" | "BLOCKED" | "DONE" | "READY:" | "BLOCKED:" | "DONE:") => true,
            Some(name) => {
                name.bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"-_.".contains(&byte))
                    && words.next() == Some("DONE")
                    && words.next().is_none()
            }
            None => false,
        }
    }
}

impl Writer {
    fn reason(&self) -> Option<&'static str> {
        match self
            .review
            .as_ref()
            .filter(|review| review.head == self.head)
        {
            Some(review) if review.verdict == "REQUEST_CHANGES" => return Some("request_changes"),
            Some(review) if review.verdict == "ACCEPT" => (),
            _ => return Some("unreviewed_head"),
        }
        let reported = self.status.as_ref().is_some_and(|status| {
            !self.status_path.is_empty() && status.path == self.status_path && status.is_report()
        });
        (!reported).then_some("missing_status")
    }

    fn alert(&self, now: u64) -> Option<Alert> {
        let since = self.idle_since?;
        let age = now.checked_sub(since)?;
        if self.busy || age < 1200 || self.session_id.is_empty() || self.host.is_empty() {
            return None;
        }
        let reason = self.reason()?;
        let id = format!(
            "writer-idle host={} session={}",
            json!(self.host),
            json!(self.session_id)
        );
        Some(Alert {
            active: format!("{id} idle_since={since}"),
            recipient: Some(self.parent.clone()).filter(|parent| !parent.is_empty()),
            message: format!(
                "{id} parent={} head={} last_commit={} idle_seconds={age} reason={reason}",
                json!(self.parent),
                json!(self.head),
                self.last_commit
            ),
        })
    }
}

impl Activity {
    pub(super) fn alerts(&self) -> Vec<Alert> {
        let mut alerts = BTreeMap::new();
        for alert in self
            .writers
            .iter()
            .filter_map(|writer| writer.alert(self.now))
        {
            alerts.entry(alert.active.clone()).or_insert(alert);
        }
        alerts.into_values().collect()
    }
}
