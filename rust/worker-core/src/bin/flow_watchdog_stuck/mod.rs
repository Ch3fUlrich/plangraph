use serde::Deserialize;

#[derive(Default, Deserialize)]
#[serde(default)]
pub struct Snapshot {
    lapsed_leases: Vec<Row>,
    ordered_items: Vec<Row>,
    ready_owners: Vec<Row>,
    idle_capacity: Vec<Row>,
    go_requests: Vec<Row>,
    merged_reviews: Vec<Row>,
}

#[derive(Deserialize)]
struct Row {
    item: String,
    owner: String,
    age_minutes: u64,
    #[serde(default)]
    open_lane: bool,
    #[serde(default)]
    unclaimed: bool,
    #[serde(default)]
    no_live_session: bool,
    #[serde(default)]
    capacity_available: bool,
    #[serde(default)]
    unanswered: bool,
    #[serde(default)]
    in_review: bool,
}

impl Snapshot {
    pub fn messages(&self) -> Vec<String> {
        type Rule<'a> = (&'static str, &'a [Row], u64, fn(&Row) -> bool);
        let rules: [Rule<'_>; 6] = [
            ("lapsed-lease", &self.lapsed_leases, 30, |r| r.open_lane),
            ("ordered-unclaimed", &self.ordered_items, 120, |r| {
                r.unclaimed
            }),
            ("owner-no-session", &self.ready_owners, 60, |r| {
                r.no_live_session
            }),
            ("idle-capacity", &self.idle_capacity, 30, |r| {
                r.capacity_available
            }),
            ("unanswered-go-req", &self.go_requests, 60, |r| r.unanswered),
            ("merged-review-open", &self.merged_reviews, 360, |r| {
                r.in_review
            }),
        ];
        let mut messages = Vec::new();
        for (name, rows, threshold, eligible) in rules {
            for row in rows {
                if row.age_minutes > threshold && eligible(row) {
                    let message = format!(
                        "{name} item={} owner={}",
                        serde_json::to_string(&row.item).unwrap(),
                        serde_json::to_string(&row.owner).unwrap()
                    );
                    if !messages.contains(&message) {
                        messages.push(message);
                    }
                }
            }
        }
        messages.sort();
        messages
    }
}
