//! The post-merge settlement of a landed train: close the parts' items, verify the host sync, close the
//! folded pull requests and stop the writers whose work is in the landed tree. The landed manifest and
//! every step result are written to a state file before the next effect runs, so a crash at any point
//! resumes where it stopped; a step that is `Done` is never run again, a step that is `Held` is checked
//! again on every run and runs once its reason has cleared. The effects (keeper, GitHub, hosts) are
//! behind [`Effects`] so the order and the refusals are tested without them.
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

/// The hosts a landing is verified on (rule 3.39: workstation, WSL and the Linux build host).
pub const HOSTS: usize = 3;

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "state", content = "note", rename_all = "snake_case")]
pub enum Step {
    #[default]
    Pending,
    /// Finished; never run again.
    Done(String),
    /// Refused for a reason that can clear (a moved pull request, unmerged writer work); checked on the
    /// next run.
    Held(String),
}

impl Step {
    pub fn is_done(&self) -> bool {
        matches!(self, Step::Done(_))
    }
}

/// One landed part. `folded` parts reached main only through the train's merge commit.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PartState {
    pub pr: u64,
    /// The part's head as the train froze it.
    pub head: String,
    pub items: Vec<String>,
    pub folded: bool,
    /// The writer's last known head and its spawner record; absent when the part had no live writer.
    #[serde(default)]
    pub writer_head: Option<String>,
    #[serde(default)]
    pub writer_record: Option<PathBuf>,
    #[serde(default)]
    pub owner: Option<String>,
    #[serde(default)]
    pub ready_at: Option<i64>,
    pub close_items: Step,
    pub close_pr: Step,
    pub stop_writer: Step,
}

/// A fact recorded as it happened: ownership, READY time, train membership, eviction, CI, host sync.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Event {
    pub at: i64,
    pub kind: String,
    pub detail: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct HostReceipt {
    pub host: String,
    pub head: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Settlement {
    pub repo: String,
    /// The train's pull request and the merge commit it produced on main.
    pub train_pr: u64,
    pub merge: String,
    pub parts: Vec<PartState>,
    pub hosts: Step,
    pub hosts_receipts: Vec<HostReceipt>,
    /// The once-per-train fold of the per-item documentation fragments into the shared references.
    #[serde(default)]
    pub docs: Step,
    /// The hold on new batches (rule 3.64) ended: every step finished and the hosts are current.
    pub hold_released: bool,
    pub events: Vec<Event>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PrState {
    Open,
    Closed,
    Merged,
}

/// What proves a part is in main: its own merge, or the train's merge for a folded part.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Proof {
    OwnMerge,
    TrainMerge { merge: String, part_head: String },
}

/// What an effect did for one part: finished it, or refused it for a reason that can clear.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Applied {
    Done,
    Kept(String),
}

/// Everything a step does outside the state file. Each method is safe to repeat.
pub trait Effects {
    fn pr_head(&mut self, pr: u64) -> Result<(PrState, String), String>;
    /// Whether `sha` is an ancestor of (or equal to) the landed merge commit.
    fn in_landed_tree(&mut self, sha: &str, merge: &str) -> Result<bool, String>;
    /// The keeper closes the part's items on merge evidence; `Kept` when it refused or flagged one.
    fn close_items(&mut self, part: &PartState, proof: &Proof) -> Result<Applied, String>;
    fn verify_hosts(&mut self, merge: &str) -> Result<Vec<HostReceipt>, String>;
    fn close_pr(&mut self, pr: u64) -> Result<(), String>;
    fn reopen_pr(&mut self, pr: u64) -> Result<(), String>;
    /// The guarded stop; `Kept` when it left the writer running.
    fn stop_writer(&mut self, part: &PartState, merge: &str) -> Result<Applied, String>;
    fn end_hold(&mut self) -> Result<(), String>;
    /// Fold the landed documentation fragments of the merge into the shared references, once per train;
    /// returns what it folded and kept. A fragment that does not fit its reference stays for the next train.
    fn fold_docs(&mut self, train_pr: u64, merge: &str) -> Result<String, String>;
}

#[derive(Debug, PartialEq, Eq)]
pub struct Summary {
    pub done: usize,
    pub held: Vec<String>,
    pub complete: bool,
}

impl Settlement {
    pub fn new(repo: &str, train_pr: u64, merge: &str, parts: Vec<PartState>) -> Settlement {
        Settlement {
            repo: repo.into(),
            train_pr,
            merge: merge.into(),
            parts,
            hosts: Step::Pending,
            hosts_receipts: vec![],
            docs: Step::Pending,
            hold_released: false,
            events: vec![],
        }
    }

    pub fn load(path: &Path) -> Result<Option<Settlement>, String> {
        match std::fs::read(path) {
            Ok(raw) => serde_json::from_slice(&raw)
                .map(Some)
                .map_err(|e| format!("settlement state {}: {e}", path.display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.to_string()),
        }
    }

    /// Replace the state file atomically: a crash leaves the old state or the new one, never half.
    pub fn save(&self, path: &Path) -> Result<(), String> {
        use std::io::Write;
        let dir = path.parent().filter(|p| !p.as_os_str().is_empty());
        let dir = dir.unwrap_or(Path::new("."));
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        let mut file = tempfile::NamedTempFile::new_in(dir).map_err(|e| e.to_string())?;
        let body = serde_json::to_vec_pretty(self).map_err(|e| e.to_string())?;
        file.write_all(&body).map_err(|e| e.to_string())?;
        file.as_file().sync_all().map_err(|e| e.to_string())?;
        file.persist(path).map_err(|e| e.error.to_string())?;
        Ok(())
    }

    fn note(&mut self, kind: &str, detail: String, now: i64) {
        self.events.push(Event {
            at: now,
            kind: kind.into(),
            detail,
        });
    }

    /// Record a fact that happened outside the settlement (CI result, eviction, READY time, ownership).
    pub fn record(&mut self, kind: &str, detail: &str, now: i64) {
        self.note(kind, detail.into(), now);
    }

    fn proof(&self, part: &PartState) -> Proof {
        if part.folded {
            Proof::TrainMerge {
                merge: self.merge.clone(),
                part_head: part.head.clone(),
            }
        } else {
            Proof::OwnMerge
        }
    }

    /// Run every open step once, saving to `path` after each result. The first effect error ends the run
    /// with the state saved; `run` again resumes.
    pub fn run(
        &mut self,
        path: &Path,
        fx: &mut dyn Effects,
        now: &dyn Fn() -> i64,
    ) -> Result<Summary, String> {
        self.save(path)?;
        for i in 0..self.parts.len() {
            self.close_items_step(i, fx, now)?;
            self.save(path)?;
        }
        if !self.hosts.is_done() {
            let receipts = fx.verify_hosts(&self.merge)?;
            let current = receipts.len() == HOSTS
                && receipts.iter().all(|r| r.head == self.merge)
                && distinct_hosts(&receipts);
            if !current {
                return Err(format!(
                    "host sync: {} receipts, expected {HOSTS} distinct hosts at {}",
                    receipts.len(),
                    self.merge
                ));
            }
            self.hosts_receipts = receipts;
            self.hosts = Step::Done(format!("{HOSTS} hosts at {}", self.merge));
            self.note("host_sync", self.merge.clone(), now());
            self.save(path)?;
        }
        for i in 0..self.parts.len() {
            self.close_pr_step(i, fx, now)?;
            self.save(path)?;
        }
        for i in 0..self.parts.len() {
            self.stop_writer_step(i, fx, now)?;
            self.save(path)?;
        }
        if !self.docs.is_done() {
            let summary = fx.fold_docs(self.train_pr, &self.merge)?;
            self.docs = Step::Done(summary.clone());
            self.note("docs_fold", summary, now());
            self.save(path)?;
        }
        let complete = self.hosts.is_done()
            && self.docs.is_done()
            && self.parts.iter().all(|p| {
                p.close_items.is_done() && p.close_pr.is_done() && p.stop_writer.is_done()
            });
        if complete && !self.hold_released {
            fx.end_hold()?;
            self.hold_released = true;
            self.note(
                "hold_released",
                "backlog landed, hosts current".into(),
                now(),
            );
            self.save(path)?;
        }
        Ok(self.summary())
    }

    pub fn summary(&self) -> Summary {
        let mut held = Vec::new();
        let mut done = 0;
        for p in &self.parts {
            for (name, step) in [
                ("close_items", &p.close_items),
                ("close_pr", &p.close_pr),
                ("stop_writer", &p.stop_writer),
            ] {
                match step {
                    Step::Done(_) => done += 1,
                    Step::Held(why) => held.push(format!("#{} {name}: {why}", p.pr)),
                    Step::Pending => {}
                }
            }
        }
        Summary {
            done,
            held,
            complete: self.hold_released,
        }
    }

    fn close_items_step(
        &mut self,
        i: usize,
        fx: &mut dyn Effects,
        now: &dyn Fn() -> i64,
    ) -> Result<(), String> {
        let part = self.parts[i].clone();
        if part.close_items.is_done() {
            return Ok(());
        }
        let proof = self.proof(&part);
        // The item stays open when its pull request moved after the freeze: the merge did not carry the
        // head the writer now has.
        let (state, head) = fx.pr_head(part.pr)?;
        if state != PrState::Merged && head != part.head {
            return self.hold(i, Held::Items, format!("pull request moved to {head}"), now);
        }
        // A folded part has no merge of its own: its head must be inside the train's merge.
        if let Proof::TrainMerge { merge, part_head } = &proof {
            if !fx.in_landed_tree(part_head, merge)? {
                return self.hold(
                    i,
                    Held::Items,
                    "part head is not in the train merge".into(),
                    now,
                );
            }
        }
        if let Some(w) = &part.writer_head {
            if !fx.in_landed_tree(w, &self.merge)? {
                return self.hold(
                    i,
                    Held::Items,
                    format!("writer has unmerged work at {w}"),
                    now,
                );
            }
        }
        if let Applied::Kept(why) = fx.close_items(&part, &proof)? {
            return self.hold(
                i,
                Held::Items,
                format!("keeper kept the item open: {why}"),
                now,
            );
        }
        self.parts[i].close_items = Step::Done(format!("closed {} item(s)", part.items.len()));
        self.note(
            "items_closed",
            format!("#{} {}", part.pr, part.items.join(",")),
            now(),
        );
        Ok(())
    }

    fn close_pr_step(
        &mut self,
        i: usize,
        fx: &mut dyn Effects,
        now: &dyn Fn() -> i64,
    ) -> Result<(), String> {
        let part = self.parts[i].clone();
        if part.close_pr.is_done() {
            return Ok(());
        }
        if !part.folded {
            // GitHub closed it with its own merge.
            self.parts[i].close_pr = Step::Done("merged on its own".into());
            return Ok(());
        }
        let (state, head) = fx.pr_head(part.pr)?;
        if state == PrState::Closed && head != part.head {
            // A crash after the close call but before the reread: a push that arrived in between is
            // still on the closed pull request. Give it back.
            fx.reopen_pr(part.pr)?;
            return self.hold(
                i,
                Held::Pr,
                format!("pushed to {head} during close; reopened"),
                now,
            );
        }
        if state != PrState::Open {
            self.parts[i].close_pr = Step::Done("already closed".into());
            return Ok(());
        }
        if head != part.head {
            return self.hold(
                i,
                Held::Pr,
                format!("pushed to {head} after the freeze; left open"),
                now,
            );
        }
        fx.close_pr(part.pr)?;
        // Closing is a separate call from the head read: reread and give the pull request back when a
        // writer pushed in between, so the push is not lost.
        let (_, after) = fx.pr_head(part.pr)?;
        if after != part.head {
            fx.reopen_pr(part.pr)?;
            return self.hold(
                i,
                Held::Pr,
                format!("pushed to {after} during close; reopened"),
                now,
            );
        }
        self.parts[i].close_pr = Step::Done(format!("closed at {}", part.head));
        self.note("pr_closed", format!("#{}", part.pr), now());
        Ok(())
    }

    fn stop_writer_step(
        &mut self,
        i: usize,
        fx: &mut dyn Effects,
        now: &dyn Fn() -> i64,
    ) -> Result<(), String> {
        let part = self.parts[i].clone();
        if part.stop_writer.is_done() {
            return Ok(());
        }
        let Some(writer) = &part.writer_head else {
            if part.writer_record.is_some() {
                return Err(format!(
                    "part #{} has a writer record but no writer head",
                    part.pr
                ));
            }
            self.parts[i].stop_writer = Step::Done("no writer".into());
            return Ok(());
        };
        if !part.close_pr.is_done() {
            return self.hold(i, Held::Writer, "pull request is still open".into(), now);
        }
        if !fx.in_landed_tree(writer, &self.merge)? {
            return self.hold(
                i,
                Held::Writer,
                format!("writer has unmerged work at {writer}"),
                now,
            );
        }
        if let Applied::Kept(why) = fx.stop_writer(&part, &self.merge)? {
            return self.hold(i, Held::Writer, format!("writer kept running: {why}"), now);
        }
        self.parts[i].stop_writer = Step::Done(format!("stopped at {writer}"));
        self.note("writer_stopped", format!("#{}", part.pr), now());
        Ok(())
    }

    fn hold(
        &mut self,
        i: usize,
        which: Held,
        why: String,
        now: &dyn Fn() -> i64,
    ) -> Result<(), String> {
        let part = &mut self.parts[i];
        let step = Step::Held(why.clone());
        let slot = match which {
            Held::Items => &mut part.close_items,
            Held::Pr => &mut part.close_pr,
            Held::Writer => &mut part.stop_writer,
        };
        let changed = *slot != step;
        *slot = step;
        if changed {
            let pr = part.pr;
            self.note("held", format!("#{pr}: {why}"), now());
        }
        Ok(())
    }
}

enum Held {
    Items,
    Pr,
    Writer,
}

fn distinct_hosts(receipts: &[HostReceipt]) -> bool {
    let mut names: Vec<&str> = receipts.iter().map(|r| r.host.as_str()).collect();
    names.sort_unstable();
    names.dedup();
    names.len() == receipts.len()
}
