//! Keeper client: the worker's only channel to orchestration.
//!
//! The real keeper service does not exist yet, so this contract is the seam:
//! an HTTP client for production and an in-memory stub for tests both
//! implement [`Keeper`].
//!
//! | Method | Path           | Request body             | Success response   |
//! |--------|----------------|--------------------------|--------------------|
//! | POST   | `/v1/claim`    | `{"worker": ..}`          | 200 `Claim` \| 204 |
//! | POST   | `/v1/worker_claim` | `{"worker": ..,"card": ..}` | 200 `Claim` \| 204 |
//! | POST   | `/v1/phase_plan` | `{"key"}`              | 200 `PhasePlan`    |
//! | POST   | `/v1/phase_finish` | `{"key","branch","head"}` | 200 `PhaseFinish` |
//! | POST   | `/v1/attempts` | `AttemptReport`          | 204                |
//! | POST   | `/v1/park`     | `ParkPacket`             | 204                |
//! | POST   | `/v1/complete` | `{"card", "merged_sha"}` | 204                |
//! | POST   | `/v1/alarms`   | `Alarm`                  | 204                |
//! | POST   | `/v1/ask_question` | `{"key","text","default_answer","deadline","risk_tags"}` | 200 `{"id",..}` |
//! | POST   | `/v1/answer_question` | `{"question_id","source":"question_timeout"}` | 200 |
//! | POST   | `/v1/locks/<repo>/acquire` | `{"owner", "ttl_s"}` | 200 `Fence`    |
//! | POST   | `/v1/locks/<repo>/release` | `Fence`              | 204            |
//!
//! `<repo>` is percent-encoded as one path segment.
//!
//! Any other status is reported as [`KeeperError::Status`].

use crate::question::Question;
use pg_worker_core::exit_class::ExitClass;
use pg_worker_core::packet::ParkPacket;
use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use std::time::Duration;

/// One unit of work handed to a worker.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Claim {
    pub card: String,
    pub repo: String,
    pub base_sha: String,
    pub check_cmd: String,
    /// Empty or `change` requires a red base; `regression` skips that gate.
    #[serde(default)]
    pub check_kind: String,
    /// Repo-relative check inputs, protected unless explicitly allowed.
    #[serde(default)]
    pub check_reads: Vec<String>,
    pub allow: Vec<String>,
    pub forbid: Vec<String>,
    pub prompt: String,
}

/// What one attempt did, sent back so the keeper can pick the next move.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttemptReport {
    pub card: String,
    pub n: u32,
    pub leg: String,
    pub exit_class: ExitClass,
    pub diff_hash: String,
    pub check_tail: String,
}

/// A condition a human must look at.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Alarm {
    pub card: String,
    pub reason: String,
}

/// Fencing token of a keeper-held merge lock. The token only ever grows, so a
/// holder whose lock expired and was re-granted presents a token the repo side
/// can tell is stale.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fence {
    pub token: u64,
    pub owner: String,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum KeeperError {
    #[error("keeper request failed: {0}")]
    Http(String),
    #[error("keeper returned unexpected status {0}")]
    Status(u16),
    #[error("keeper response could not be decoded: {0}")]
    Decode(String),
}

/// The worker's view of orchestration. Implemented by [`HttpKeeper`] against
/// the real service and by [`StubKeeper`] in tests.
pub trait Keeper: Send + Sync {
    fn claim_next(&self, worker: &str) -> Result<Option<Claim>, KeeperError>;
    /// Claim one named card for the phase runner; `None` means the keeper has nothing for it (not ready / taken).
    fn claim_card(&self, worker: &str, card: &str) -> Result<Option<Claim>, KeeperError>;
    /// Read-only plan of phase `key`; the keeper never writes for it.
    fn phase_plan(&self, key: &str) -> Result<crate::phase::PhasePlan, KeeperError>;
    /// Land the finished phase branch (push, PR, merge on green CI); the keeper uses its own credential.
    fn phase_finish(
        &self,
        key: &str,
        branch: &str,
        head: &str,
    ) -> Result<crate::phase::PhaseFinish, KeeperError>;
    fn report_attempt(&self, r: &AttemptReport) -> Result<(), KeeperError>;
    fn park(&self, p: &ParkPacket) -> Result<(), KeeperError>;
    fn complete(&self, card: &str, merged_sha: &str) -> Result<(), KeeperError>;
    fn alarm(&self, a: &Alarm) -> Result<(), KeeperError>;
    /// Ask the keeper for the merge lock for `repo`, valid for `ttl_s`
    /// seconds. It is exclusive across hosts only insofar as a real keeper
    /// service implements the lock endpoint (RELEASE-DEPLOY gap); the worker's
    /// effective exclusion is the file lock in `mergelock`, single host.
    fn acquire_merge_lock(&self, repo: &str, owner: &str, ttl_s: u64)
    -> Result<Fence, KeeperError>;
    /// Give the lock back. A stale token is refused.
    fn release_merge_lock(&self, repo: &str, fence: &Fence) -> Result<(), KeeperError>;
    /// Record the agent's question for card `key`; returns the question id. The
    /// question's `deadline_s` counts from now.
    fn ask_question(&self, key: &str, q: &Question) -> Result<i64, KeeperError>;
    /// Record that the question's deadline passed and its stored default was
    /// taken (`source: question_timeout`). Needs a router or system token.
    fn record_question_timeout(&self, question_id: i64) -> Result<(), KeeperError>;
}

/// Percent-encode `s` so it is one URL path segment (unreserved bytes pass).
fn encode_segment(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// Blocking HTTP implementation of [`Keeper`] over ureq.
pub struct HttpKeeper {
    base: String,
    agent: ureq::Agent,
    /// Bearer token the keeper requires on every verb; `None` sends none.
    token: Option<String>,
}

impl HttpKeeper {
    fn claim_with(
        &self,
        path: &str,
        payload: serde_json::Value,
    ) -> Result<Option<Claim>, KeeperError> {
        let url = self.url(path);
        match self.post(&url).send_json(payload) {
            Ok(resp) => match resp.status() {
                200 => {
                    let claim: Claim = resp
                        .into_json()
                        .map_err(|e| KeeperError::Decode(e.to_string()))?;
                    Ok(Some(claim))
                }
                204 => Ok(None),
                code => Err(KeeperError::Status(code)),
            },
            Err(ureq::Error::Status(code, _)) => Err(KeeperError::Status(code)),
            Err(e) => Err(KeeperError::Http(e.to_string())),
        }
    }

    /// `base_url` may carry a trailing slash; it is stripped so paths can be
    /// appended verbatim.
    pub fn new(base_url: &str) -> Self {
        let base = base_url.trim_end_matches('/').to_string();
        let agent = ureq::AgentBuilder::new()
            .timeout(Duration::from_secs(10))
            .build();
        HttpKeeper {
            base,
            agent,
            token: None,
        }
    }

    /// Send `Authorization: Bearer <token>` on every request. An empty token
    /// counts as none, so an unset `PG_KEEPER_TOKEN=` never sends a blank header.
    pub fn with_token(mut self, token: &str) -> Self {
        self.token = Some(token.trim().to_string()).filter(|t| !t.is_empty());
        self
    }

    /// A POST request to `url`, carrying the bearer token when one is set.
    fn post(&self, url: &str) -> ureq::Request {
        let req = self.agent.post(url);
        match &self.token {
            Some(t) => req.set("Authorization", &format!("Bearer {t}")),
            None => req,
        }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base, path)
    }

    /// POST a JSON body whose accepted success code is 200 with a JSON reply.
    fn post_json(
        &self,
        path: &str,
        body: &impl Serialize,
    ) -> Result<serde_json::Value, KeeperError> {
        let url = self.url(path);
        match self.post(&url).send_json(body) {
            Ok(resp) if resp.status() == 200 => resp
                .into_json()
                .map_err(|e| KeeperError::Decode(e.to_string())),
            Ok(resp) => Err(KeeperError::Status(resp.status())),
            Err(ureq::Error::Status(code, _)) => Err(KeeperError::Status(code)),
            Err(e) => Err(KeeperError::Http(e.to_string())),
        }
    }

    /// POST a JSON body whose only accepted success code is 204.
    fn post204(&self, path: &str, body: &impl Serialize) -> Result<(), KeeperError> {
        let url = self.url(path);
        match self.post(&url).send_json(body) {
            Ok(resp) if resp.status() == 204 => Ok(()),
            Ok(resp) => Err(KeeperError::Status(resp.status())),
            Err(ureq::Error::Status(code, _)) => Err(KeeperError::Status(code)),
            Err(e) => Err(KeeperError::Http(e.to_string())),
        }
    }
}

impl Keeper for HttpKeeper {
    fn claim_card(&self, worker: &str, card: &str) -> Result<Option<Claim>, KeeperError> {
        self.claim_with(
            "/v1/worker_claim",
            serde_json::json!({ "worker": worker, "card": card }),
        )
    }

    fn phase_plan(&self, key: &str) -> Result<crate::phase::PhasePlan, KeeperError> {
        let value = self.post_json("/v1/phase_plan", &serde_json::json!({ "key": key }))?;
        if !value.is_object() {
            return Err(KeeperError::Decode(
                "phase_plan reply is not an object".into(),
            ));
        }
        serde_json::from_value::<crate::phase::PhasePlan>(value)
            .map_err(|e| KeeperError::Decode(e.to_string()))
    }

    fn phase_finish(
        &self,
        key: &str,
        branch: &str,
        head: &str,
    ) -> Result<crate::phase::PhaseFinish, KeeperError> {
        let value = self.post_json(
            "/v1/phase_finish",
            &serde_json::json!({ "key": key, "branch": branch, "head": head }),
        )?;
        if !value.is_object() {
            return Err(KeeperError::Decode(
                "phase_finish reply is not an object".into(),
            ));
        }
        serde_json::from_value::<crate::phase::PhaseFinish>(value)
            .map_err(|e| KeeperError::Decode(e.to_string()))
    }

    fn claim_next(&self, worker: &str) -> Result<Option<Claim>, KeeperError> {
        self.claim_with("/v1/claim", serde_json::json!({ "worker": worker }))
    }

    fn report_attempt(&self, r: &AttemptReport) -> Result<(), KeeperError> {
        self.post204("/v1/attempts", r)
    }

    fn park(&self, p: &ParkPacket) -> Result<(), KeeperError> {
        self.post204("/v1/park", p)
    }

    fn complete(&self, card: &str, merged_sha: &str) -> Result<(), KeeperError> {
        let payload = serde_json::json!({ "card": card, "merged_sha": merged_sha });
        self.post204("/v1/complete", &payload)
    }

    fn alarm(&self, a: &Alarm) -> Result<(), KeeperError> {
        self.post204("/v1/alarms", a)
    }

    fn acquire_merge_lock(
        &self,
        repo: &str,
        owner: &str,
        ttl_s: u64,
    ) -> Result<Fence, KeeperError> {
        let url = self.url(&format!("/v1/locks/{}/acquire", encode_segment(repo)));
        let payload = serde_json::json!({ "owner": owner, "ttl_s": ttl_s });
        match self.post(&url).send_json(payload) {
            Ok(resp) if resp.status() == 200 => resp
                .into_json()
                .map_err(|e| KeeperError::Decode(e.to_string())),
            Ok(resp) => Err(KeeperError::Status(resp.status())),
            Err(ureq::Error::Status(code, _)) => Err(KeeperError::Status(code)),
            Err(e) => Err(KeeperError::Http(e.to_string())),
        }
    }

    fn release_merge_lock(&self, repo: &str, fence: &Fence) -> Result<(), KeeperError> {
        let path = format!("/v1/locks/{}/release", encode_segment(repo));
        self.post204(&path, fence)
    }

    fn ask_question(&self, key: &str, q: &Question) -> Result<i64, KeeperError> {
        let deadline = q.deadline_s.map(|s| {
            crate::attempt::iso_utc(std::time::SystemTime::now() + Duration::from_secs(s))
        });
        let payload = serde_json::json!({
            "key": key,
            "text": q.text,
            "default_answer": q.default,
            "deadline": deadline,
            "risk_tags": q.risk_tags,
        });
        let v = self.post_json("/v1/ask_question", &payload)?;
        v.get("id")
            .and_then(serde_json::Value::as_i64)
            .ok_or_else(|| KeeperError::Decode("ask_question reply has no integer id".into()))
    }

    fn record_question_timeout(&self, question_id: i64) -> Result<(), KeeperError> {
        let payload = serde_json::json!({
            "question_id": question_id,
            "source": "question_timeout",
        });
        self.post_json("/v1/answer_question", &payload).map(|_| ())
    }
}

/// In-memory [`Keeper`] for tests: claims come from a queue the test fills, and
/// every other call is recorded for later assertions.
#[derive(Default)]
pub struct StubKeeper {
    inner: Mutex<StubState>,
}

#[derive(Default)]
struct StubState {
    card_claims: Vec<Claim>,
    plans: Vec<crate::phase::PhasePlan>,
    plan_calls: Vec<String>,
    finishes: Vec<crate::phase::PhaseFinish>,
    finish_calls: Vec<(String, String, String)>,
    claim_card_calls: Vec<(String, String)>,
    queue: Vec<Claim>,
    attempts: Vec<AttemptReport>,
    parked: Vec<ParkPacket>,
    completed: Vec<(String, String)>,
    alarms: Vec<Alarm>,
    /// Highest token handed out; the next acquire returns `last_token + 1`.
    last_token: u64,
    /// The fence currently held, if any.
    held: Option<Fence>,
    lock_calls: Vec<String>,
    lock_unavailable: bool,
    questions: Vec<(String, Question)>,
    question_timeouts: Vec<i64>,
    /// When set, `record_question_timeout` is refused like a 403.
    timeout_refused: bool,
    /// When set, `record_question_timeout` fails with this status (a 5xx here
    /// is a transient outage); checked before `timeout_refused`.
    timeout_status: Option<u16>,
    /// When set, `record_question_timeout` panics (a keeper client that dies
    /// instead of returning an error), so the activity itself fails.
    timeout_panics: bool,
    /// When set, `alarm` fails with a 500 (the alarm is not recorded).
    alarm_down: bool,
    /// When set, `ask_question` fails with this status every time.
    ask_status: Option<u16>,
}

impl StubKeeper {
    pub fn push_card_claim(&self, c: Claim) {
        self.lock().card_claims.push(c);
    }

    pub fn push_plan(&self, p: crate::phase::PhasePlan) {
        self.lock().plans.push(p);
    }

    pub fn plan_calls(&self) -> Vec<String> {
        self.lock().plan_calls.clone()
    }

    pub fn push_finish(&self, f: crate::phase::PhaseFinish) {
        self.lock().finishes.push(f);
    }

    pub fn finish_calls(&self) -> Vec<(String, String, String)> {
        self.lock().finish_calls.clone()
    }

    pub fn claim_card_calls(&self) -> Vec<(String, String)> {
        self.lock().claim_card_calls.clone()
    }

    /// Make every `acquire_merge_lock` fail, as an unreachable keeper would.
    pub fn set_lock_unavailable(&self, down: bool) {
        self.lock().lock_unavailable = down;
    }

    /// Simulate the held lock expiring and being re-granted elsewhere: the
    /// current token goes stale and the next one is already spoken for.
    pub fn expire_merge_lock(&self) {
        let mut s = self.lock();
        s.held = None;
        s.last_token += 1;
    }

    /// Lock calls in order, as `acquire:<repo>:<token>` / `release:<repo>:<token>`.
    pub fn lock_calls(&self) -> Vec<String> {
        self.lock().lock_calls.clone()
    }

    /// True while a fence is outstanding.
    pub fn merge_lock_held(&self) -> bool {
        self.lock().held.is_some()
    }

    /// Questions asked so far as `(key, question)`; ids are 1-based positions.
    pub fn questions(&self) -> Vec<(String, Question)> {
        self.lock().questions.clone()
    }

    /// Ids passed to `record_question_timeout`, in order (refused calls included).
    pub fn question_timeouts(&self) -> Vec<i64> {
        self.lock().question_timeouts.clone()
    }

    /// Make `record_question_timeout` fail with a 403, as a keeper would for a
    /// worker token without the router or system role.
    pub fn set_timeout_refused(&self, refused: bool) {
        self.lock().timeout_refused = refused;
    }

    /// Make `record_question_timeout` fail with `status` every time.
    pub fn set_timeout_status(&self, status: Option<u16>) {
        self.lock().timeout_status = status;
    }

    /// Make `record_question_timeout` panic every time (the activity errors
    /// instead of returning "not recorded").
    pub fn set_timeout_panics(&self, panics: bool) {
        self.lock().timeout_panics = panics;
    }

    /// Make `ask_question` fail with `status` every time (a 5xx is an outage).
    pub fn set_ask_status(&self, status: Option<u16>) {
        self.lock().ask_status = status;
    }

    /// Make every `alarm` call fail, as an unreachable alarm sink would.
    pub fn set_alarm_down(&self, down: bool) {
        self.lock().alarm_down = down;
    }

    pub fn push_claim(&self, c: Claim) {
        self.lock().queue.push(c);
    }

    pub fn attempts(&self) -> Vec<AttemptReport> {
        self.lock().attempts.clone()
    }

    pub fn parked(&self) -> Vec<ParkPacket> {
        self.lock().parked.clone()
    }

    pub fn completed(&self) -> Vec<(String, String)> {
        self.lock().completed.clone()
    }

    pub fn alarms(&self) -> Vec<Alarm> {
        self.lock().alarms.clone()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, StubState> {
        self.inner.lock().expect("stub keeper mutex poisoned")
    }
}

impl Keeper for StubKeeper {
    fn claim_card(&self, worker: &str, card: &str) -> Result<Option<Claim>, KeeperError> {
        let mut state = self.lock();
        state
            .claim_card_calls
            .push((worker.to_string(), card.to_string()));
        let pos = state.card_claims.iter().position(|c| c.card == card);
        match pos {
            Some(i) => Ok(Some(state.card_claims.remove(i))),
            None => Ok(None),
        }
    }

    fn phase_plan(&self, key: &str) -> Result<crate::phase::PhasePlan, KeeperError> {
        let mut state = self.lock();
        state.plan_calls.push(key.to_string());
        if state.plans.is_empty() {
            return Err(KeeperError::Status(404));
        }
        if state.plans.len() == 1 {
            Ok(state.plans[0].clone())
        } else {
            Ok(state.plans.remove(0))
        }
    }

    fn phase_finish(
        &self,
        key: &str,
        branch: &str,
        head: &str,
    ) -> Result<crate::phase::PhaseFinish, KeeperError> {
        let mut state = self.lock();
        state
            .finish_calls
            .push((key.to_string(), branch.to_string(), head.to_string()));
        if state.finishes.is_empty() {
            return Err(KeeperError::Status(404));
        }
        if state.finishes.len() == 1 {
            Ok(state.finishes[0].clone())
        } else {
            Ok(state.finishes.remove(0))
        }
    }

    fn claim_next(&self, _worker: &str) -> Result<Option<Claim>, KeeperError> {
        let mut state = self.lock();
        if state.queue.is_empty() {
            Ok(None)
        } else {
            Ok(Some(state.queue.remove(0)))
        }
    }

    fn report_attempt(&self, r: &AttemptReport) -> Result<(), KeeperError> {
        self.lock().attempts.push(r.clone());
        Ok(())
    }

    fn park(&self, p: &ParkPacket) -> Result<(), KeeperError> {
        self.lock().parked.push(p.clone());
        Ok(())
    }

    fn complete(&self, card: &str, merged_sha: &str) -> Result<(), KeeperError> {
        self.lock()
            .completed
            .push((card.to_string(), merged_sha.to_string()));
        Ok(())
    }

    fn alarm(&self, a: &Alarm) -> Result<(), KeeperError> {
        let mut s = self.lock();
        if s.alarm_down {
            return Err(KeeperError::Status(500));
        }
        s.alarms.push(a.clone());
        Ok(())
    }

    fn acquire_merge_lock(
        &self,
        repo: &str,
        owner: &str,
        _ttl_s: u64,
    ) -> Result<Fence, KeeperError> {
        let mut s = self.lock();
        if s.lock_unavailable {
            return Err(KeeperError::Http("keeper unavailable".into()));
        }
        s.last_token += 1;
        let fence = Fence {
            token: s.last_token,
            owner: owner.to_string(),
        };
        s.held = Some(fence.clone());
        s.lock_calls.push(format!("acquire:{repo}:{}", fence.token));
        Ok(fence)
    }

    fn release_merge_lock(&self, repo: &str, fence: &Fence) -> Result<(), KeeperError> {
        let mut s = self.lock();
        s.lock_calls.push(format!("release:{repo}:{}", fence.token));
        match &s.held {
            Some(h) if h == fence => {
                s.held = None;
                Ok(())
            }
            // Stale (expired or re-granted) or never held: refused like a 409.
            _ => Err(KeeperError::Status(409)),
        }
    }

    fn ask_question(&self, key: &str, q: &Question) -> Result<i64, KeeperError> {
        let mut s = self.lock();
        if let Some(code) = s.ask_status {
            return Err(KeeperError::Status(code));
        }
        s.questions.push((key.to_string(), q.clone()));
        Ok(s.questions.len() as i64)
    }

    fn record_question_timeout(&self, question_id: i64) -> Result<(), KeeperError> {
        let mut s = self.lock();
        s.question_timeouts.push(question_id);
        if s.timeout_panics {
            drop(s); // never poison the stub for the other callers
            panic!("stub keeper: record_question_timeout died");
        }
        if let Some(code) = s.timeout_status {
            Err(KeeperError::Status(code))
        } else if s.timeout_refused {
            Err(KeeperError::Status(403))
        } else {
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pg_worker_core::config::Tier;
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::thread::JoinHandle;

    fn claim() -> Claim {
        Claim {
            card: "c-1".into(),
            repo: "org/repo".into(),
            base_sha: "abc123".into(),
            check_cmd: "cargo test".into(),
            allow: vec!["src/**".into()],
            forbid: vec![".env".into()],
            prompt: "do the thing".into(),
            ..Default::default()
        }
    }

    #[test]
    fn old_claim_json_defaults_check_metadata() {
        let old = r#"{"card":"c","repo":"r","base_sha":"abc","check_cmd":"false","allow":[],"forbid":[],"prompt":"p"}"#;
        let c: Claim = serde_json::from_str(old).unwrap();
        assert!(c.check_kind.is_empty());
        assert!(c.check_reads.is_empty());
    }

    fn report() -> AttemptReport {
        AttemptReport {
            card: "c-1".into(),
            n: 2,
            leg: "free".into(),
            exit_class: ExitClass::CheckFail,
            diff_hash: "deadbeef".into(),
            check_tail: "boom".into(),
        }
    }

    fn park() -> ParkPacket {
        ParkPacket {
            card: "c-1".into(),
            tier: Tier::Free,
            exit_class: ExitClass::CheckFail,
            check_tail: "boom".into(),
        }
    }

    fn alarm() -> Alarm {
        Alarm {
            card: "c-1".into(),
            reason: "stuck".into(),
        }
    }

    #[test]
    fn stub_keeper_round_trip() {
        let keeper = StubKeeper::default();
        assert_eq!(keeper.claim_next("w-1").unwrap(), None);

        let c = claim();
        keeper.push_claim(c.clone());
        assert_eq!(keeper.claim_next("w-1").unwrap(), Some(c));
        assert_eq!(keeper.claim_next("w-1").unwrap(), None);

        let r = report();
        keeper.report_attempt(&r).unwrap();
        assert_eq!(keeper.attempts(), vec![r.clone()]);

        let p = park();
        keeper.park(&p).unwrap();
        assert_eq!(keeper.parked(), vec![p.clone()]);

        assert_eq!(keeper.completed(), Vec::<(String, String)>::new());
        keeper.complete("c-1", "sha-1").unwrap();
        assert_eq!(
            keeper.completed(),
            vec![("c-1".to_string(), "sha-1".to_string())]
        );

        let a = alarm();
        keeper.alarm(&a).unwrap();
        assert_eq!(keeper.alarms(), vec![a]);
    }

    /// A request as the one-shot test server observed it.
    struct Captured {
        head: String,
        method: String,
        path: String,
        body: String,
    }

    /// Bind a one-shot HTTP server on 127.0.0.1:0. It accepts a single
    /// request, reads it fully (headers plus `Content-Length` body), replies
    /// with the canned status line and body, and yields what it saw.
    fn serve_once(status: u16, reason: &str, body: &str) -> (String, JoinHandle<Captured>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
        let addr = listener.local_addr().expect("read local addr");
        let reason = reason.to_string();
        let body = body.to_string();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept one request");
            let captured = read_request(&mut stream);
            let mut response = format!("HTTP/1.1 {status} {reason}\r\n");
            // 204 carries no Content-Length and no body by definition.
            if status != 204 {
                response.push_str(&format!("Content-Length: {}\r\n", body.len()));
            }
            response.push_str("Content-Type: application/json\r\nConnection: close\r\n\r\n");
            response.push_str(&body);
            stream
                .write_all(response.as_bytes())
                .expect("write response");
            stream.flush().expect("flush response");
            captured
        });
        (format!("http://{addr}"), handle)
    }

    fn read_request(stream: &mut TcpStream) -> Captured {
        // Headers end at the first blank line; read a byte at a time so a
        // request split across TCP segments is still captured whole.
        let mut raw = Vec::new();
        let mut byte = [0u8; 1];
        while !raw.ends_with(b"\r\n\r\n") {
            match stream.read(&mut byte) {
                Ok(0) => break,
                Ok(_) => raw.push(byte[0]),
                Err(_) => break,
            }
        }
        let head = String::from_utf8_lossy(&raw).to_string();
        let request_line = head.lines().next().unwrap_or_default();
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or_default().to_string();
        let path = parts.next().unwrap_or_default().to_string();
        let content_length = head
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                if name.eq_ignore_ascii_case("content-length") {
                    value.trim().parse::<usize>().ok()
                } else {
                    None
                }
            })
            .unwrap_or(0);
        let mut body = vec![0u8; content_length];
        if content_length > 0 {
            stream.read_exact(&mut body).expect("read request body");
        }
        Captured {
            head,
            method,
            path,
            body: String::from_utf8_lossy(&body).to_string(),
        }
    }

    #[test]
    fn http_claim_200_returns_claim() {
        let c = claim();
        let body = serde_json::to_string(&c).unwrap();
        let (base, server) = serve_once(200, "OK", &body);
        let keeper = HttpKeeper::new(&base);

        assert_eq!(keeper.claim_next("w-1").unwrap(), Some(c));

        let captured = server.join().expect("server thread");
        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/claim");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&captured.body).unwrap(),
            serde_json::json!({ "worker": "w-1" })
        );
    }

    #[test]
    fn http_token_is_sent_as_a_bearer_header_only_when_set() {
        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base)
            .with_token(" s3cret \n")
            .report_attempt(&report())
            .unwrap();
        let captured = server.join().expect("server thread");
        assert!(
            captured.head.contains("authorization: Bearer s3cret\r\n")
                || captured.head.contains("Authorization: Bearer s3cret\r\n"),
            "{}",
            captured.head
        );

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base)
            .with_token("  ")
            .report_attempt(&report())
            .unwrap();
        let captured = server.join().expect("server thread");
        assert!(!captured.head.to_lowercase().contains("authorization"));
    }

    #[test]
    fn http_claim_card_posts_worker_and_card() {
        let body = serde_json::to_string(&claim()).unwrap();
        let (base, handle) = serve_once(200, "OK", &body);
        let keeper = HttpKeeper::new(&base);

        let got = keeper.claim_card("w-1", "c-9").expect("claim_card");
        let captured = handle.join().expect("server thread");

        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/worker_claim");
        let sent: serde_json::Value = serde_json::from_str(&captured.body).unwrap();
        assert_eq!(sent, serde_json::json!({"worker": "w-1", "card": "c-9"}));
        assert_eq!(got, Some(claim()));
    }

    #[test]
    fn http_claim_card_204_is_none() {
        let (base, handle) = serve_once(204, "No Content", "");
        let keeper = HttpKeeper::new(&base);

        let got = keeper.claim_card("w-1", "c-9").expect("claim_card");
        let captured = handle.join().expect("server thread");

        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/worker_claim");
        assert_eq!(got, None);
    }

    #[test]
    fn http_phase_plan_posts_key_and_parses() {
        let body = r#"{
            "phase": "demo:PH-X",
            "ready": true,
            "cards": [{
                "key": "A",
                "write_set": ["src/a.rs"],
                "route": {
                    "window_class": "128k",
                    "harness": "opencode",
                    "legs": ["l1"]
                },
                "extra": 1
            }]
        }"#;
        let (base, handle) = serve_once(200, "OK", body);
        let keeper = HttpKeeper::new(&base);

        let plan = keeper.phase_plan("demo:PH-X").expect("phase_plan");
        let captured = handle.join().expect("server thread");

        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/phase_plan");
        let sent: serde_json::Value = serde_json::from_str(&captured.body).unwrap();
        assert_eq!(sent, serde_json::json!({"key": "demo:PH-X"}));

        let plan = serde_json::to_value(&plan).unwrap();
        assert_eq!(plan["phase"], serde_json::json!("demo:PH-X"));
        assert_eq!(plan["ready"], serde_json::json!(true));
        assert_eq!(plan["cards"][0]["key"], serde_json::json!("A"));
        assert_eq!(
            plan["cards"][0]["write_set"],
            serde_json::json!(["src/a.rs"])
        );
        assert_eq!(plan["cards"][0]["route"]["legs"], serde_json::json!(["l1"]));
    }

    #[test]
    fn http_phase_plan_bad_body_is_decode_error() {
        let (base, handle) = serve_once(200, "OK", "[]");
        let keeper = HttpKeeper::new(&base);

        let err = keeper.phase_plan("demo:PH-X").unwrap_err();
        let captured = handle.join().expect("server thread");

        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/phase_plan");
        assert!(matches!(err, KeeperError::Decode(_)));
    }

    #[test]
    fn http_phase_plan_403_is_status() {
        let (base, handle) = serve_once(403, "Forbidden", "");
        let keeper = HttpKeeper::new(&base);

        let err = keeper.phase_plan("demo:PH-X").unwrap_err();
        let captured = handle.join().expect("server thread");

        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/phase_plan");
        assert!(matches!(err, KeeperError::Status(403)));
    }

    #[test]
    fn stub_card_claims_and_plans() {
        let keeper = StubKeeper::default();

        let mut one = claim();
        one.card = "c-1".to_string();
        let mut two = claim();
        two.card = "c-2".to_string();
        let mut one_again = claim();
        one_again.card = "c-1".to_string();

        keeper.push_card_claim(one);
        keeper.push_card_claim(two);
        keeper.push_card_claim(one_again);

        let mut expected_two = claim();
        expected_two.card = "c-2".to_string();
        assert_eq!(
            keeper.claim_card("w-1", "c-2").expect("claim_card"),
            Some(expected_two)
        );
        assert!(
            keeper
                .claim_card("w-1", "missing")
                .expect("claim_card")
                .is_none()
        );

        let mut expected_one = claim();
        expected_one.card = "c-1".to_string();
        assert_eq!(
            keeper.claim_card("w-1", "c-1").expect("claim_card"),
            Some(expected_one)
        );

        let mut expected_one_again = claim();
        expected_one_again.card = "c-1".to_string();
        assert_eq!(
            keeper.claim_card("w-1", "c-1").expect("claim_card"),
            Some(expected_one_again)
        );
        assert!(
            keeper
                .claim_card("w-1", "c-1")
                .expect("claim_card")
                .is_none()
        );

        assert_eq!(
            keeper.claim_card_calls(),
            vec![
                ("w-1".to_string(), "c-2".to_string()),
                ("w-1".to_string(), "missing".to_string()),
                ("w-1".to_string(), "c-1".to_string()),
                ("w-1".to_string(), "c-1".to_string()),
                ("w-1".to_string(), "c-1".to_string()),
            ]
        );

        assert!(matches!(
            keeper.phase_plan("demo:PH-X"),
            Err(KeeperError::Status(404))
        ));

        let p1: crate::phase::PhasePlan = serde_json::from_str(
            r#"{"phase":"demo:PH-1","ready":true,"cards":[],"route":{"legs":[]}}"#,
        )
        .unwrap();
        let p2: crate::phase::PhasePlan = serde_json::from_str(
            r#"{"phase":"demo:PH-2","ready":false,"cards":[],"route":{"legs":[]}}"#,
        )
        .unwrap();
        keeper.push_plan(p1.clone());
        keeper.push_plan(p2.clone());

        assert_eq!(keeper.phase_plan("demo:PH-1").unwrap(), p1);
        assert_eq!(keeper.phase_plan("demo:PH-2").unwrap(), p2);
        assert_eq!(keeper.phase_plan("demo:PH-2").unwrap(), p2);

        assert_eq!(
            keeper.plan_calls(),
            vec![
                "demo:PH-X".to_string(),
                "demo:PH-1".to_string(),
                "demo:PH-2".to_string(),
                "demo:PH-2".to_string(),
            ]
        );
    }

    #[test]
    fn http_claim_204_is_none() {
        let (base, server) = serve_once(204, "No Content", "");
        let keeper = HttpKeeper::new(&base);

        assert_eq!(keeper.claim_next("w-1").unwrap(), None);
        server.join().expect("server thread");
    }

    #[test]
    fn http_report_attempt_posts_to_attempts() {
        let r = report();
        let (base, server) = serve_once(204, "No Content", "");
        // A trailing slash on the base must not double up in the path.
        let keeper = HttpKeeper::new(&format!("{base}/"));

        keeper.report_attempt(&r).unwrap();

        let captured = server.join().expect("server thread");
        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/attempts");
        let sent: AttemptReport = serde_json::from_str(&captured.body).unwrap();
        assert_eq!(sent, r);
        assert!(captured.body.contains("\"exit_class\":\"check_fail\""));
    }

    #[test]
    fn http_status_error_maps_to_status() {
        let (base, server) = serve_once(500, "Internal Server Error", "");
        let keeper = HttpKeeper::new(&base);

        let err = keeper.report_attempt(&report()).unwrap_err();
        assert_eq!(err, KeeperError::Status(500));
        server.join().expect("server thread");
    }

    #[test]
    fn http_remaining_endpoints() {
        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base).park(&park()).unwrap();
        assert_eq!(server.join().unwrap().path, "/v1/park");

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base).complete("c-1", "sha-1").unwrap();
        let captured = server.join().unwrap();
        assert_eq!(captured.path, "/v1/complete");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&captured.body).unwrap(),
            serde_json::json!({ "card": "c-1", "merged_sha": "sha-1" })
        );

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base).alarm(&alarm()).unwrap();
        assert_eq!(server.join().unwrap().path, "/v1/alarms");
    }

    #[test]
    fn stub_lock_bumps_tokens_and_rejects_stale_release() {
        let k = StubKeeper::default();
        let a = k.acquire_merge_lock("org/repo", "w-1", 60).unwrap();
        let b = k.acquire_merge_lock("org/repo", "w-2", 60).unwrap();
        assert!(b.token > a.token);
        // `a` was superseded by `b`: releasing it is refused.
        assert_eq!(
            k.release_merge_lock("org/repo", &a).unwrap_err(),
            KeeperError::Status(409)
        );
        k.release_merge_lock("org/repo", &b).unwrap();
        assert!(!k.merge_lock_held());
        k.set_lock_unavailable(true);
        assert!(matches!(
            k.acquire_merge_lock("org/repo", "w-1", 60),
            Err(KeeperError::Http(_))
        ));
        assert_eq!(
            k.lock_calls(),
            vec![
                "acquire:org/repo:1".to_string(),
                "acquire:org/repo:2".to_string(),
                "release:org/repo:1".to_string(),
                "release:org/repo:2".to_string(),
            ]
        );
    }

    #[test]
    fn http_acquire_posts_and_decodes_the_fence() {
        let fence = Fence {
            token: 7,
            owner: "w-1".into(),
        };
        let (base, server) = serve_once(200, "OK", &serde_json::to_string(&fence).unwrap());
        let got = HttpKeeper::new(&base)
            .acquire_merge_lock("org/repo", "w-1", 90)
            .unwrap();
        assert_eq!(got, fence);
        let captured = server.join().unwrap();
        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/locks/org%2Frepo/acquire");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&captured.body).unwrap(),
            serde_json::json!({ "owner": "w-1", "ttl_s": 90 })
        );
    }

    #[test]
    fn http_release_posts_the_fence() {
        let fence = Fence {
            token: 7,
            owner: "w-1".into(),
        };
        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base)
            .release_merge_lock("org/repo", &fence)
            .unwrap();
        let captured = server.join().unwrap();
        assert_eq!(captured.path, "/v1/locks/org%2Frepo/release");
        assert_eq!(
            serde_json::from_str::<Fence>(&captured.body).unwrap(),
            fence
        );
    }

    #[test]
    fn http_lock_errors_map_like_the_other_verbs() {
        let (base, server) = serve_once(409, "Conflict", "");
        assert_eq!(
            HttpKeeper::new(&base)
                .acquire_merge_lock("r", "w", 1)
                .unwrap_err(),
            KeeperError::Status(409)
        );
        server.join().unwrap();

        let (base, server) = serve_once(200, "OK", "not json");
        assert!(matches!(
            HttpKeeper::new(&base).acquire_merge_lock("r", "w", 1),
            Err(KeeperError::Decode(_))
        ));
        server.join().unwrap();

        let (base, server) = serve_once(500, "Internal Server Error", "");
        let f = Fence {
            token: 1,
            owner: "w".into(),
        };
        assert_eq!(
            HttpKeeper::new(&base)
                .release_merge_lock("r", &f)
                .unwrap_err(),
            KeeperError::Status(500)
        );
        server.join().unwrap();
    }

    fn question() -> Question {
        Question {
            text: "which db?".into(),
            default: Some("sqlite".into()),
            deadline_s: Some(60),
            risk_tags: Vec::new(),
        }
    }

    #[test]
    fn http_ask_question_posts_the_inbox_shape_and_reads_the_id() {
        let (base, server) = serve_once(200, "OK", r#"{"id":7,"key":"c-1","state":"open"}"#);
        let id = HttpKeeper::new(&base)
            .ask_question("c-1", &question())
            .unwrap();
        assert_eq!(id, 7);
        let got = server.join().unwrap();
        assert_eq!(
            (got.method.as_str(), got.path.as_str()),
            ("POST", "/v1/ask_question")
        );
        let body: serde_json::Value = serde_json::from_str(&got.body).unwrap();
        assert_eq!(body["key"], "c-1");
        assert_eq!(body["text"], "which db?");
        assert_eq!(body["default_answer"], "sqlite");
        assert!(body["deadline"].as_str().unwrap().ends_with('Z'));
        assert_eq!(body["risk_tags"], serde_json::json!([]));
    }

    #[test]
    fn http_record_question_timeout_names_the_source_and_surfaces_a_refusal() {
        let (base, server) = serve_once(200, "OK", r#"{"question_id":7,"state":"timeout"}"#);
        HttpKeeper::new(&base).record_question_timeout(7).unwrap();
        let got = server.join().unwrap();
        assert_eq!(got.path, "/v1/answer_question");
        let body: serde_json::Value = serde_json::from_str(&got.body).unwrap();
        assert_eq!(
            body,
            serde_json::json!({"question_id": 7, "source": "question_timeout"})
        );

        let (base, server) = serve_once(403, "Forbidden", r#"{"error":"forbidden"}"#);
        assert_eq!(
            HttpKeeper::new(&base).record_question_timeout(7),
            Err(KeeperError::Status(403))
        );
        server.join().unwrap();
    }

    #[test]
    fn stub_records_questions_and_timeouts() {
        let k = StubKeeper::default();
        assert_eq!(k.ask_question("c-1", &question()).unwrap(), 1);
        assert_eq!(k.ask_question("c-1", &question()).unwrap(), 2);
        k.record_question_timeout(2).unwrap();
        k.set_timeout_refused(true);
        assert_eq!(k.record_question_timeout(1), Err(KeeperError::Status(403)));
        assert_eq!(k.questions().len(), 2);
        assert_eq!(k.question_timeouts(), vec![2, 1]);
    }

    #[test]
    fn http_phase_finish_posts_key_branch_head() {
        let body = r#"{
            "phase": "demo:PH-X",
            "branch": "phase/p1",
            "head": "abc",
            "cards": [],
            "state": "merged",
            "pr": {"number": 7, "url": "u"},
            "detail": ""
        }"#;
        let (base, handle) = serve_once(200, "OK", body);
        let keeper = HttpKeeper::new(&base);

        let got = keeper
            .phase_finish("p1", "phase/p1", "abc")
            .expect("phase_finish");
        let captured = handle.join().expect("server thread");

        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/phase_finish");
        let sent: serde_json::Value = serde_json::from_str(&captured.body).expect("json");
        assert_eq!(
            sent,
            serde_json::json!({"key":"p1","branch":"phase/p1","head":"abc"})
        );
        assert_eq!(got.state, "merged");
        assert_eq!(got.pr.as_ref().map(|p| p.number), Some(7));
    }

    #[test]
    fn http_phase_finish_403_is_status() {
        let (base, handle) = serve_once(403, "Forbidden", "");
        let keeper = HttpKeeper::new(&base);

        let err = keeper
            .phase_finish("p1", "phase/p1", "abc")
            .expect_err("phase_finish");
        let captured = handle.join().expect("server thread");

        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/phase_finish");
        assert!(matches!(err, KeeperError::Status(403)), "{err:?}");
    }

    #[test]
    fn stub_finish_queue_and_calls() {
        let keeper = StubKeeper::default();
        assert!(matches!(
            keeper.phase_finish("p1", "phase/p1", "h"),
            Err(KeeperError::Status(404))
        ));

        let finish = crate::phase::PhaseFinish {
            state: crate::phase::FINISH_MERGED.to_string(),
            pr: None,
            detail: String::new(),
        };
        keeper.push_finish(finish.clone());

        for _ in 0..2 {
            let got = keeper.phase_finish("p1", "phase/p1", "h").unwrap();
            assert_eq!(got.state, finish.state);
            assert_eq!(got.pr, finish.pr);
            assert_eq!(got.detail, finish.detail);
        }
        // Refused/empty-queue calls are recorded too, so the initial 404 call
        // contributes an entry in addition to the two successful finishes.
        assert_eq!(
            keeper.finish_calls(),
            vec![
                ("p1".to_string(), "phase/p1".to_string(), "h".to_string()),
                ("p1".to_string(), "phase/p1".to_string(), "h".to_string()),
                ("p1".to_string(), "phase/p1".to_string(), "h".to_string()),
            ]
        );
    }
}
