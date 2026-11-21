//! Resolution by request (conflict note P8, V12 to V15): the append rules of the keeper's resolve events.
//!
//! A request has at most one open submission; a correction by the batch's recorded writer before the
//! deadline replaces it (`superseded`); every append carries an idempotency key scoped to
//! (principal, repository, verb) and bound to its canonical payload, and names the state it expects.
//! `replay` applies a recorded log with the same rules, so a verifier and the keeper cannot disagree.
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Time a request stays open after the keeper stamped it (seconds).
pub const DEADLINE_SECS: i64 = 30 * 60;
/// A submission still pending after this long may end its build as `service_failure` (seconds).
pub const PENDING_BOUND_SECS: i64 = 60 * 60;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "verb", rename_all = "snake_case")]
pub enum Event {
    /// The builder: the merge of `head` onto `onto` conflicts at `position` of `build`.
    Request {
        build: String,
        position: u32,
        part: u64,
        head: String,
        onto: String,
    },
    /// The batch's writer: a candidate merge commit. `replaces` names the open submission it means to
    /// replace, or is `None` when it expects none.
    Submit {
        request: String,
        replaces: Option<String>,
        commit: String,
    },
    /// The registrar: the candidate object is pinned.
    Secured { submission: String },
    /// The registrar: the candidate is confirmed gone.
    NeverSecured { submission: String },
    /// The builder: the secured candidate fails a deterministic step.
    Reject { submission: String, reason: String },
    /// The builder: the secured candidate passes both steps.
    Adopt { submission: String },
    /// The registrar: brings a secured submission of an earlier request with the same two parents.
    Carry { from: String, to: String },
    /// The builder: the request ran out without an adopted candidate.
    TimedOut { request: String },
    /// The builder: ends a build; `service_failure` names the pending submission that blocks it.
    Discard {
        build: String,
        reason: String,
        submission: Option<String>,
    },
}

impl Event {
    fn verb(&self) -> &'static str {
        match self {
            Event::Request { .. } => "request",
            Event::Submit { .. } => "submit",
            Event::Secured { .. } => "secured",
            Event::NeverSecured { .. } => "never_secured",
            Event::Reject { .. } => "reject",
            Event::Adopt { .. } => "adopt",
            Event::Carry { .. } => "carry",
            Event::TimedOut { .. } => "timed_out",
            Event::Discard { .. } => "discard",
        }
    }
    /// The only principal allowed to append this verb (a submit is checked against the recorded writer).
    fn principal(&self) -> Option<&'static str> {
        match self {
            Event::Submit { .. } => None,
            Event::Secured { .. } | Event::NeverSecured { .. } | Event::Carry { .. } => {
                Some("registrar")
            }
            _ => Some("builder"),
        }
    }
}

/// One append: who, where, the caller's idempotency key, the event (its expected state included).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Append {
    pub principal: String,
    pub repo: String,
    pub key: String,
    pub event: Event,
    /// The keeper's clock at the append (epoch seconds).
    pub now: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Outcome {
    Request { id: String, deadline: i64 },
    Submission { id: String },
    Done,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Refusal {
    /// The same key came with another payload in the same scope.
    KeyReuse,
    /// A repeated request names another part, head or prefix.
    RequestMismatch,
    /// The state the caller expected does not hold.
    ExpectationFailed(String),
    /// The principal may not append this verb (or is not the batch's recorded writer).
    NotAllowed,
    PastDeadline,
    /// The request or build is already closed, or the named object is unknown.
    Closed(String),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Status {
    Pending,
    Secured,
    NeverSecured,
    Rejected,
    Superseded,
    Adopted,
}

struct Submission {
    request: String,
    commit: String,
    stamped: i64,
    status: Status,
    ever_secured: bool,
}

struct Request {
    build: String,
    position: u32,
    part: u64,
    head: String,
    onto: String,
    deadline: i64,
    open: Option<String>,
    adopted: Option<String>,
    timed_out: bool,
}

/// An adopted resolution, as a manifest lists it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Adopted {
    pub request: String,
    pub build: String,
    pub position: u32,
    pub part: u64,
    pub head: String,
    pub onto: String,
    pub submission: String,
    pub commit: String,
}

type Scope = (String, String, &'static str, String);

#[derive(Default)]
pub struct Ledger {
    /// build -> the batch's recorded writer
    writers: BTreeMap<String, String>,
    requests: BTreeMap<String, Request>,
    by_position: BTreeMap<(String, String, u32), String>,
    submissions: BTreeMap<String, Submission>,
    terminal: BTreeMap<String, String>,
    keys: BTreeMap<Scope, (String, Result<Outcome, Refusal>)>,
    next: u32,
}

fn expect(ok: bool, what: &str) -> Result<(), Refusal> {
    if ok {
        Ok(())
    } else {
        Err(Refusal::ExpectationFailed(what.into()))
    }
}

impl Ledger {
    pub fn new(writers: BTreeMap<String, String>) -> Ledger {
        Ledger {
            writers,
            ..Ledger::default()
        }
    }

    fn id(&mut self, prefix: &str) -> String {
        self.next += 1;
        format!("{prefix}{}", self.next)
    }

    /// Appends one event. A repeat of a key in its scope returns the first result and changes nothing;
    /// the same key with another payload is refused.
    pub fn append(&mut self, a: &Append) -> Result<Outcome, Refusal> {
        let scope: Scope = (
            a.principal.clone(),
            a.repo.clone(),
            a.event.verb(),
            a.key.clone(),
        );
        let payload = serde_json::to_string(&a.event).unwrap_or_default();
        if let Some((first, result)) = self.keys.get(&scope) {
            return if *first == payload {
                result.clone()
            } else {
                Err(Refusal::KeyReuse)
            };
        }
        let result = self.apply(a);
        self.keys.insert(scope, (payload, result.clone()));
        result
    }

    fn apply(&mut self, a: &Append) -> Result<Outcome, Refusal> {
        if let Some(only) = a.event.principal() {
            if a.principal != only {
                return Err(Refusal::NotAllowed);
            }
        }
        let now = a.now;
        match &a.event {
            Event::Request {
                build,
                position,
                part,
                head,
                onto,
            } => {
                if self.terminal.contains_key(build) {
                    return Err(Refusal::Closed("build has ended".into()));
                }
                let slot = (a.repo.clone(), build.clone(), *position);
                if let Some(id) = self.by_position.get(&slot) {
                    let r = &self.requests[id];
                    // A position of one build has one prefix: the repeat is the same request or an error.
                    return if r.part == *part && r.head == *head && r.onto == *onto {
                        Ok(Outcome::Request {
                            id: id.clone(),
                            deadline: r.deadline,
                        })
                    } else {
                        Err(Refusal::RequestMismatch)
                    };
                }
                let id = self.id("r");
                let deadline = now + DEADLINE_SECS;
                self.by_position.insert(slot, id.clone());
                self.requests.insert(
                    id.clone(),
                    Request {
                        build: build.clone(),
                        position: *position,
                        part: *part,
                        head: head.clone(),
                        onto: onto.clone(),
                        deadline,
                        open: None,
                        adopted: None,
                        timed_out: false,
                    },
                );
                Ok(Outcome::Request { id, deadline })
            }
            Event::Submit {
                request,
                replaces,
                commit,
            } => {
                let r = self
                    .requests
                    .get(request)
                    .ok_or_else(|| Refusal::Closed("unknown request".into()))?;
                if r.adopted.is_some() || r.timed_out {
                    return Err(Refusal::Closed("request is resolved".into()));
                }
                if self.writers.get(&r.build) != Some(&a.principal) {
                    return Err(Refusal::NotAllowed);
                }
                if now > r.deadline {
                    return Err(Refusal::PastDeadline);
                }
                expect(
                    r.open == *replaces,
                    "the open submission is not the one named",
                )?;
                let old = r.open.clone();
                let id = self.id("s");
                if let Some(old) = old {
                    if let Some(s) = self.submissions.get_mut(&old) {
                        s.status = Status::Superseded; // out for good
                    }
                }
                self.submissions.insert(
                    id.clone(),
                    Submission {
                        request: request.clone(),
                        commit: commit.clone(),
                        stamped: now,
                        status: Status::Pending,
                        ever_secured: false,
                    },
                );
                if let Some(r) = self.requests.get_mut(request) {
                    r.open = Some(id.clone());
                }
                Ok(Outcome::Submission { id })
            }
            Event::Secured { submission } => {
                self.decide(submission, Status::Pending, Status::Secured)
            }
            Event::NeverSecured { submission } => {
                self.decide(submission, Status::Pending, Status::NeverSecured)
            }
            Event::Reject { submission, .. } => {
                self.decide(submission, Status::Secured, Status::Rejected)
            }
            Event::Adopt { submission } => {
                self.decide(submission, Status::Secured, Status::Adopted)
            }
            Event::Carry { from, to } => {
                let src = self
                    .submissions
                    .get(from)
                    .ok_or_else(|| Refusal::Closed("unknown submission".into()))?;
                let (src_req, src_commit, was) = (
                    &self.requests[&src.request],
                    src.commit.clone(),
                    src.ever_secured,
                );
                let target = self
                    .requests
                    .get(to)
                    .ok_or_else(|| Refusal::Closed("unknown request".into()))?;
                if target.adopted.is_some() || target.timed_out {
                    return Err(Refusal::Closed("request is resolved".into()));
                }
                if now > target.deadline {
                    return Err(Refusal::PastDeadline);
                }
                expect(
                    target.open.is_none(),
                    "a submission is open: a carry does not supersede",
                )?;
                expect(was, "only a secured submission is carried")?;
                expect(
                    src_req.onto == target.onto && src_req.head == target.head,
                    "the two parents differ",
                )?;
                let id = self.id("s");
                self.submissions.insert(
                    id.clone(),
                    Submission {
                        request: to.clone(),
                        commit: src_commit,
                        stamped: now,
                        status: Status::Secured,
                        ever_secured: true,
                    },
                );
                if let Some(r) = self.requests.get_mut(to) {
                    r.open = Some(id.clone());
                }
                Ok(Outcome::Submission { id })
            }
            Event::TimedOut { request } => {
                let r = self
                    .requests
                    .get_mut(request)
                    .ok_or_else(|| Refusal::Closed("unknown request".into()))?;
                expect(now > r.deadline, "the deadline has not passed")?;
                expect(
                    r.adopted.is_none() && !r.timed_out,
                    "the request is resolved",
                )?;
                expect(r.open.is_none(), "a submission is open")?;
                r.timed_out = true;
                Ok(Outcome::Done)
            }
            Event::Discard {
                build,
                reason,
                submission,
            } => {
                if self.terminal.contains_key(build) {
                    return if self.terminal[build] == *reason {
                        Ok(Outcome::Done)
                    } else {
                        Err(Refusal::Closed("build already ended".into()))
                    };
                }
                if reason == "service_failure" {
                    let id = submission
                        .as_ref()
                        .ok_or_else(|| Refusal::ExpectationFailed("names no submission".into()))?;
                    let s = self
                        .submissions
                        .get(id)
                        .ok_or_else(|| Refusal::Closed("unknown submission".into()))?;
                    let r = &self.requests[&s.request];
                    expect(r.build == *build, "the submission belongs to another build")?;
                    expect(
                        r.open.as_deref() == Some(id.as_str())
                            && r.adopted.is_none()
                            && !r.timed_out,
                        "not the open submission of an unresolved request",
                    )?;
                    expect(
                        s.status == Status::Pending,
                        "the submission has a registrar result",
                    )?;
                    expect(
                        now - s.stamped > PENDING_BOUND_SECS,
                        "the submission is younger than the bound",
                    )?;
                }
                self.terminal.insert(build.clone(), reason.clone());
                Ok(Outcome::Done)
            }
        }
    }

    /// Moves the open submission `id` from `from` to `to`; superseded and decided ones are refused.
    fn decide(&mut self, id: &str, from: Status, to: Status) -> Result<Outcome, Refusal> {
        let s = self
            .submissions
            .get(id)
            .ok_or_else(|| Refusal::Closed("unknown submission".into()))?;
        let req = s.request.clone();
        let open = self.requests[&req].open.as_deref() == Some(id);
        expect(
            open && s.status == from,
            "the submission is not the request's open one in the expected state",
        )?;
        if to == Status::Adopted {
            expect(
                self.requests[&req].adopted.is_none(),
                "another candidate is adopted",
            )?;
        }
        if let Some(s) = self.submissions.get_mut(id) {
            s.status = to;
            s.ever_secured |= matches!(to, Status::Secured);
        }
        if let Some(r) = self.requests.get_mut(&req) {
            if to == Status::Adopted {
                r.adopted = Some(id.to_owned());
            }
            if matches!(
                to,
                Status::NeverSecured | Status::Rejected | Status::Adopted
            ) {
                r.open = None;
            }
        }
        Ok(Outcome::Done)
    }

    /// The adopted resolutions of one build, by position.
    pub fn adopted(&self, build: &str) -> Vec<Adopted> {
        let mut out: Vec<Adopted> = self
            .requests
            .iter()
            .filter(|(_, r)| r.build == build)
            .filter_map(|(id, r)| {
                let sub = r.adopted.as_ref()?;
                Some(Adopted {
                    request: id.clone(),
                    build: r.build.clone(),
                    position: r.position,
                    part: r.part,
                    head: r.head.clone(),
                    onto: r.onto.clone(),
                    submission: sub.clone(),
                    commit: self.submissions[sub].commit.clone(),
                })
            })
            .collect();
        out.sort_by_key(|a| a.position);
        out
    }

    /// A request that timed out, with its position (the evidence for a `conflict` left-out part).
    pub fn timed_out(&self, build: &str) -> Vec<(u64, u32)> {
        self.requests
            .values()
            .filter(|r| r.build == build && r.timed_out)
            .map(|r| (r.part, r.position))
            .collect()
    }
}

/// A verifier's replay: every recorded append is applied by the same rules, in order. Each recorded result
/// must equal the replayed one; a log that contains an append the rules refuse (or that recorded another
/// outcome) is refused with the position of the first bad append.
pub fn replay(
    writers: BTreeMap<String, String>,
    log: &[(Append, Result<Outcome, Refusal>)],
) -> Result<Ledger, usize> {
    let mut ledger = Ledger::new(writers);
    for (i, (append, recorded)) in log.iter().enumerate() {
        if ledger.append(append) != *recorded {
            return Err(i);
        }
    }
    Ok(ledger)
}
