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
//! | GET    | `/v1/show`     | query `key=<card>&fmt=json` | 200 `{"key","state",..}` (the state observation) |
//! | POST   | `/v1/phase_plan` | `{"key"}`              | 200 `PhasePlan`    |
//! | POST   | `/v1/phase_finish` | `{"key","branch","head"}` | 200 `PhaseFinish` |
//! | POST   | `/v1/attempts` | `AttemptReport` + `workflow_id`/`run_id`/`attempt` (all three or none; no `expected_state`) | 204 |
//! | POST   | `/v1/park`     | `ParkPacket` (+ `attempt`, `expected_state` when known)             | 204                |
//! | POST   | `/v1/complete` | `{"card","merged_sha"}` + `workflow_id`/`run_id`/`attempt` (+ `expected_state` when the activity input carries one) | 204 |
//! | POST   | `/v1/alarms`   | `Alarm` (`{"card","reason","attempt"?}`) + `workflow_id`/`run_id`/`attempt` (no `expected_state`) | 204 |
//! | POST   | `/v1/ask_question` | `{"key","text","default_answer","deadline","risk_tags"}` + `workflow_id`/`run_id`/`attempt` + `expected_state` (the observed state, else the live set) | 200 `{"id",..}` |
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
use std::io::Read;
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
    #[serde(default)]
    pub smoke_checks: Vec<String>,
    #[serde(default)]
    pub exports: Vec<String>,
    #[serde(default)]
    pub heldout_cmd: String,
    #[serde(default)]
    pub risk_tagged: bool,
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
    /// The card attempt number this alarm belongs to (monotone per run), not
    /// Temporal's activity retry count. Unset on legacy inputs.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attempt: Option<u32>,
}

/// The fallback `expected_state` a worker park and question ask sends when the
/// run observed nothing: the states a card may be in while this run still owns
/// it (`workflow.rs` replaces it with the state its `observe` activity captured
/// at decision time whenever that answered one -- EXEC-OBSERVED-STATE).
///
/// Worker-run writes may precede state projection. The run/attempt fence rejects
/// stale retries; this CAS preserves newer actor states and excludes the agent
/// lease (`claimed`) and the terminal ones (`parked`, `failed`, `done`,
/// `cancelled`).
///
/// `waiting` is live, not excluded: the projector writes it after an attempt
/// ends `usage_limit` or `outage`, and on `TIMER_STARTED`, so a wait-cap park or
/// a question asked after a timer would otherwise get `409 exec_stale_state`
/// until the workflow's retries ran out. Stale-duplicate protection comes from
/// the run/attempt fence plus the keeper's idempotent duplicate-park success,
/// never from leaving `waiting` out of this list.
///
/// The keeper still refuses this fallback on a `waiting`/`needs_input` card a
/// projector event wrote while the run was away: it only says "I observed
/// nothing", and a park closes the card. An observing run sends what it
/// captured instead ([`expected_state_for`]).
pub const WORKER_LIVE_STATES: &[&str] = &["open", "ready", "running", "waiting", "needs_input"];

/// The `expected_state` a park or ask sends (EXEC-OBSERVED-STATE): the state the
/// workflow observed when it decided to park or ask (the `observe` activity's
/// answer, carried in the activity input), otherwise
/// the live-set `fallback` the workflow handed over.
///
/// An observation is only usable while it names a live state: `claimed` is not
/// in `SETTABLE_STATES` (it names a claim, so the keeper's phase-holder rule is
/// the CAS for it), and a terminal state would only turn a stale verb into a
/// different refusal. Those observations fall back, exactly like a worker that
/// read nothing.
pub fn expected_state_for(
    observed: Option<String>,
    fallback: Option<Vec<String>>,
) -> Option<Vec<String>> {
    match observed {
        Some(state) if WORKER_LIVE_STATES.contains(&state.as_str()) => Some(vec![state]),
        _ => fallback,
    }
}

/// Which execution a keeper call belongs to: the workflow, this run of it, and
/// the card attempt number (monotone per run — deliberately *not* Temporal's
/// `ActivityInfo::attempt`, which is the activity retry count and resets per
/// activity). The Python keeper refuses a call whose `run_id` differs from the
/// recorded one or whose `attempt` is lower, so park, complete, alarm and
/// ask_question carry it whenever the activity knows it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ExecId {
    pub workflow_id: Option<String>,
    pub run_id: Option<String>,
    pub attempt: Option<u32>,
    /// The state this call expects: the observation the workflow captured when
    /// it decided to park or ask (carried in the activity input), or the
    /// live-set fallback when it observed nothing. Absent
    /// for legacy callers and alarms. Roll out keeper support before deploying
    /// this worker.
    pub expected_state: Option<Vec<String>>,
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
    /// The card's state as the keeper holds it right now: the observation a
    /// park CAS presents (EXEC-OBSERVED-STATE), read by the workflow's `observe`
    /// activity at decision time and carried in the `park` input. Best effort by
    /// design -- `None` means "observed nothing" (an unreachable keeper, an
    /// older keeper, an unreadable or oversized body (G3-5), a read past its
    /// 5 s timeout (G3-4)), and the caller then falls back to
    /// [`WORKER_LIVE_STATES`] instead of failing the call. Blocking (ureq): call
    /// it from `spawn_blocking`, never on a tokio worker thread (G3-4).
    fn card_state(&self, card: &str) -> Option<String>;
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
    fn report_attempt(&self, r: &AttemptReport, exec: &ExecId) -> Result<(), KeeperError>;
    fn park(&self, p: &ParkPacket, exec: &ExecId) -> Result<(), KeeperError>;
    fn complete(&self, card: &str, merged_sha: &str, exec: &ExecId) -> Result<(), KeeperError>;
    fn alarm(&self, a: &Alarm, exec: &ExecId) -> Result<(), KeeperError>;
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
    fn ask_question(&self, key: &str, q: &Question, exec: &ExecId) -> Result<i64, KeeperError>;
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

/// Merge the execution identity into a JSON object body: `workflow_id`,
/// `run_id` and `attempt` are inserted together or not at all, so a body whose
/// exec is unknown or only partially known goes out exactly as it did before
/// (a legacy body, never a half-tagged one).
fn with_exec(mut body: serde_json::Value, exec: &ExecId) -> serde_json::Value {
    // Independent of execution fencing; deploy the keeper before the worker.
    if let (Some(obj), Some(state)) = (body.as_object_mut(), &exec.expected_state) {
        let value = if state.len() == 1 {
            serde_json::json!(state[0])
        } else {
            serde_json::json!(state)
        };
        obj.insert("expected_state".into(), value);
    }
    let (Some(workflow_id), Some(run_id), Some(attempt)) =
        (&exec.workflow_id, &exec.run_id, exec.attempt)
    else {
        return body;
    };
    if let Some(obj) = body.as_object_mut() {
        obj.insert("workflow_id".into(), workflow_id.as_str().into());
        obj.insert("run_id".into(), run_id.as_str().into());
        obj.insert("attempt".into(), attempt.into());
    }
    body
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

    /// A GET request to `url`, carrying the bearer token when one is set.
    fn get(&self, url: &str) -> ureq::Request {
        let req = self.agent.get(url);
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

/// Largest `GET /v1/show` body the worker buffers for a card-state observation
/// (G3-5): the show JSON is a few hundred bytes, so anything past this cap is
/// answered `None` ("observed nothing") instead of being read whole.
const CARD_STATE_MAX_BODY: u64 = 64 * 1024;

impl Keeper for HttpKeeper {
    fn card_state(&self, card: &str) -> Option<String> {
        // `fmt=json`: `show` defaults to its text form. Anything this read
        // cannot answer is "observed nothing" -- the live-set fallback stands.
        let path = format!("/v1/show?key={}&fmt=json", encode_segment(card));
        let resp = match self.get(&self.url(&path)).call() {
            Ok(resp) if resp.status() == 200 => resp,
            Ok(_) | Err(_) => return None,
        };
        // G3-5: buffer at most CARD_STATE_MAX_BODY + 1 bytes; a larger body is
        // refused as unreadable instead of being buffered whole.
        let mut buf = Vec::new();
        resp.into_reader()
            .take(CARD_STATE_MAX_BODY + 1)
            .read_to_end(&mut buf)
            .ok()?;
        if buf.len() as u64 > CARD_STATE_MAX_BODY {
            return None;
        }
        let value: serde_json::Value = serde_json::from_slice(&buf).ok()?;
        value
            .get("state")
            .and_then(|s| s.as_str())
            .map(str::to_owned)
    }

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

    fn report_attempt(&self, r: &AttemptReport, exec: &ExecId) -> Result<(), KeeperError> {
        let body = serde_json::to_value(r).expect("attempt report serializes");
        let attempt_exec = ExecId {
            expected_state: None,
            ..exec.clone()
        };
        self.post204("/v1/attempts", &with_exec(body, &attempt_exec))
    }

    fn park(&self, p: &ParkPacket, exec: &ExecId) -> Result<(), KeeperError> {
        let body = serde_json::to_value(p).expect("park packet serializes");
        self.post204("/v1/park", &with_exec(body, exec))
    }

    fn complete(&self, card: &str, merged_sha: &str, exec: &ExecId) -> Result<(), KeeperError> {
        let payload = serde_json::json!({ "card": card, "merged_sha": merged_sha });
        self.post204("/v1/complete", &with_exec(payload, exec))
    }

    fn alarm(&self, a: &Alarm, exec: &ExecId) -> Result<(), KeeperError> {
        let body = serde_json::to_value(a).expect("alarm serializes");
        let alarm_exec = ExecId {
            expected_state: None,
            ..exec.clone()
        };
        self.post204("/v1/alarms", &with_exec(body, &alarm_exec))
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

    fn ask_question(&self, key: &str, q: &Question, exec: &ExecId) -> Result<i64, KeeperError> {
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
        let v = self.post_json("/v1/ask_question", &with_exec(payload, exec))?;
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
    /// Execution identities paired with attempt reports, in report order.
    attempt_execs: Vec<ExecId>,
    /// The [`ExecId`] of every park/complete/alarm/ask_question call, in call
    /// order (failed calls included: they were calls too).
    execs: Vec<ExecId>,
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
    /// What `card_state` reports. `None` (the default) is a keeper the worker
    /// observed nothing on, so park and ask keep the live-set fallback.
    card_state: Option<String>,
    /// The cards `card_state` was asked for, in order.
    card_state_calls: Vec<String>,
    /// Make `card_state` hang that long first (G3-4: a stalled read).
    card_state_delay: Option<Duration>,
}

impl StubKeeper {
    /// Make `card_state` answer `state` (`None` again to observe nothing).
    pub fn set_card_state(&self, state: Option<&str>) {
        self.lock().card_state = state.map(str::to_owned);
    }

    /// Cards `card_state` was asked for, in order.
    pub fn card_state_calls(&self) -> Vec<String> {
        self.lock().card_state_calls.clone()
    }

    /// Make `card_state` sleep that long before answering (a stalled read).
    pub fn set_card_state_delay(&self, delay: Option<Duration>) {
        self.lock().card_state_delay = delay;
    }

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

    /// The exec identity of every park/complete/alarm/ask_question call, in
    /// call order.
    pub fn execs(&self) -> Vec<ExecId> {
        self.lock().execs.clone()
    }

    /// Execution identities of reports, separate from final state-change calls.
    pub fn attempt_execs(&self) -> Vec<ExecId> {
        self.lock().attempt_execs.clone()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, StubState> {
        self.inner.lock().expect("stub keeper mutex poisoned")
    }
}

impl Keeper for StubKeeper {
    fn card_state(&self, card: &str) -> Option<String> {
        let delay = {
            let mut state = self.lock();
            state.card_state_calls.push(card.to_string());
            state.card_state_delay
        };
        if let Some(delay) = delay {
            std::thread::sleep(delay);
        }
        self.lock().card_state.clone()
    }

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

    fn report_attempt(&self, r: &AttemptReport, exec: &ExecId) -> Result<(), KeeperError> {
        let mut s = self.lock();
        s.attempt_execs.push(ExecId {
            expected_state: None,
            ..exec.clone()
        });
        s.attempts.push(r.clone());
        Ok(())
    }

    fn park(&self, p: &ParkPacket, exec: &ExecId) -> Result<(), KeeperError> {
        let mut s = self.lock();
        s.execs.push(exec.clone());
        // The keeper's state CAS, stub-equivalent (EXEC-OBSERVED-STATE): an
        // expectation that misses the state the stub holds is refused 409, so a
        // park that presents a stale observation can be tested without a server.
        // No expectation (or no held state) keeps the unconditional stub park.
        if let (Some(expected), Some(state)) = (&exec.expected_state, &s.card_state)
            && !expected.iter().any(|e| e == state)
        {
            return Err(KeeperError::Status(409));
        }
        s.parked.push(p.clone());
        Ok(())
    }

    fn complete(&self, card: &str, merged_sha: &str, exec: &ExecId) -> Result<(), KeeperError> {
        let mut s = self.lock();
        s.execs.push(exec.clone());
        s.completed.push((card.to_string(), merged_sha.to_string()));
        Ok(())
    }

    fn alarm(&self, a: &Alarm, exec: &ExecId) -> Result<(), KeeperError> {
        let mut s = self.lock();
        s.execs.push(exec.clone());
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

    fn ask_question(&self, key: &str, q: &Question, exec: &ExecId) -> Result<i64, KeeperError> {
        let mut s = self.lock();
        s.execs.push(exec.clone());
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
    #[test]
    fn live_states_include_waiting_and_exclude_agent_lease_and_terminal_states() {
        assert_eq!(
            super::WORKER_LIVE_STATES,
            &["open", "ready", "running", "waiting", "needs_input"]
        );
        // The projector writes `waiting` (usage_limit/outage attempt ends and
        // TIMER_STARTED), so a park or ask after a timer must accept it.
        assert!(super::WORKER_LIVE_STATES.contains(&"waiting"));
        for state in ["claimed", "parked", "failed", "done", "cancelled"] {
            assert!(!super::WORKER_LIVE_STATES.contains(&state));
        }
    }

    /// EXEC-OBSERVED-STATE: the state the run observed is what park and ask
    /// send; the live set stands only when there was no usable observation.
    #[test]
    fn expected_state_is_the_observation_or_the_live_set_fallback() {
        let live = || {
            super::WORKER_LIVE_STATES
                .iter()
                .map(|s| (*s).to_owned())
                .collect::<Vec<_>>()
        };
        // A projector-written `waiting` the run read is the CAS it presents,
        // so the wait-cap park after usage_limit/outage still lands.
        assert_eq!(
            super::expected_state_for(Some("waiting".to_owned()), Some(live())),
            Some(vec!["waiting".to_owned()])
        );
        // `claimed` names a claim, not a settable state, and a terminal state
        // would only swap one refusal for another: both keep the fallback.
        for observed in ["claimed", "parked", "failed", "done", "cancelled"] {
            assert_eq!(
                super::expected_state_for(Some(observed.to_owned()), Some(live())),
                Some(live()),
                "{observed} is not an observation the keeper accepts"
            );
        }
        assert_eq!(super::expected_state_for(None, Some(live())), Some(live()));
        assert_eq!(super::expected_state_for(None, None), None);
    }

    /// The observation the worker reads: `GET /v1/show?key=<card>&fmt=json`.
    #[test]
    fn http_card_state_reads_the_show_json() {
        let (base, server) = serve_once(200, "OK", r#"{"key":"c-1","state":"waiting"}"#);
        let keeper = HttpKeeper::new(&base).with_token(" s3cret ");
        assert_eq!(keeper.card_state("c-1").as_deref(), Some("waiting"));
        let captured = server.join().expect("server thread");
        assert_eq!(captured.method, "GET");
        assert_eq!(captured.path, "/v1/show?key=c-1&fmt=json");
        assert!(
            captured
                .head
                .to_ascii_lowercase()
                .contains("authorization: bearer s3cret\r\n"),
            "{}",
            captured.head
        );
    }

    /// A keeper that cannot be read is "observed nothing": the call still goes
    /// out, with the live-set fallback it already carried.
    #[test]
    fn http_card_state_that_cannot_be_read_is_no_observation() {
        let (base, server) = serve_once(404, "Not Found", r#"{"error":"not_found"}"#);
        assert_eq!(HttpKeeper::new(&base).card_state("c-1"), None);
        server.join().expect("server thread");

        let (base, server) = serve_once(200, "OK", r#"{"key":"c-1"}"#);
        assert_eq!(HttpKeeper::new(&base).card_state("c-1"), None);
        server.join().expect("server thread");
    }

    /// G3-5: a show body past the cap is answered "observed nothing" instead
    /// of being buffered whole.
    #[test]
    fn http_card_state_oversized_body_is_no_observation() {
        let pad = "x".repeat(super::CARD_STATE_MAX_BODY as usize + 1);
        let body = format!(r#"{{"key":"c-1","state":"waiting","pad":"{pad}"}}"#);
        let (base, server) = serve_once(200, "OK", &body);
        assert_eq!(HttpKeeper::new(&base).card_state("c-1"), None);
        server.join().expect("server thread");
    }

    /// G3-2: the wait-cap park after usage_limit/outage presents the
    /// observation the workflow recorded when it decided, so it lands even
    /// while `GET /v1/show` -- the read the old park path depended on -- fails.
    #[test]
    fn http_cap_park_lands_while_show_fails() {
        let (base, server) = serve_n(2, |req| {
            if req.method == "GET" {
                (500, r#"{"error":"show is down"}"#.to_string())
            } else {
                (204, String::new())
            }
        });
        let keeper = HttpKeeper::new(&base);
        // Reading the card now fails: no observation could be made this way...
        assert_eq!(keeper.card_state("c-1"), None);
        // ...but the park carries the observation the workflow recorded, and
        // the park path never retries the read.
        let exec = ExecId {
            expected_state: Some(vec!["waiting".to_string()]),
            ..exec()
        };
        keeper.park(&park(), &exec).expect("cap-park lands");
        let seen = server.join().expect("server thread");
        assert_eq!(seen.len(), 2);
        assert_eq!(seen[1].method, "POST");
        assert_eq!(seen[1].path, "/v1/park");
        assert_eq!(body_of(&seen[1])["expected_state"], "waiting");
    }

    /// The stub observes nothing until a test says otherwise, so every stub
    /// driven park and ask keeps the fallback the workflow handed over.
    #[test]
    fn stub_card_state_starts_absent_and_records_the_reads() {
        let keeper = StubKeeper::default();
        assert_eq!(keeper.card_state("c-1"), None);
        keeper.set_card_state(Some("waiting"));
        assert_eq!(keeper.card_state("c-2").as_deref(), Some("waiting"));
        assert_eq!(
            keeper.card_state_calls(),
            vec!["c-1".to_string(), "c-2".to_string()]
        );
    }

    #[test]
    fn expected_state_is_optional_in_state_call_bodies() {
        for state in [
            None,
            Some(vec!["running".to_owned()]),
            Some(
                super::WORKER_LIVE_STATES
                    .iter()
                    .map(|s| (*s).to_owned())
                    .collect(),
            ),
        ] {
            for verb in ["park", "complete", "ask_question"] {
                let (base, server) = serve_once(
                    if verb == "ask_question" { 200 } else { 204 },
                    "OK",
                    if verb == "ask_question" {
                        r#"{"id":1}"#
                    } else {
                        ""
                    },
                );
                let keeper = HttpKeeper::new(&base);
                let exec = ExecId {
                    expected_state: state.clone(),
                    ..exec()
                };
                match verb {
                    "park" => keeper.park(&park(), &exec).unwrap(),
                    "complete" => keeper.complete("c-1", "sha", &exec).unwrap(),
                    _ => {
                        keeper.ask_question("c-1", &question(), &exec).unwrap();
                    }
                }
                let body = body_of(&server.join().unwrap());
                match &state {
                    None => assert!(body.get("expected_state").is_none()),
                    Some(states) if states.len() == 1 => {
                        assert_eq!(body["expected_state"], "running")
                    }
                    Some(_) => assert_eq!(
                        body["expected_state"],
                        serde_json::json!(["open", "ready", "running", "waiting", "needs_input"])
                    ),
                }
            }
        }
    }

    #[test]
    fn http_exec_identity_and_expected_state_on_fenced_calls() {
        for state in [None, Some(vec!["running".to_string()])] {
            for verb in [
                "report_attempt",
                "park",
                "complete",
                "ask_question",
                "alarm",
            ] {
                let (base, server) = serve_once(
                    if verb == "ask_question" { 200 } else { 204 },
                    "OK",
                    if verb == "ask_question" {
                        r#"{"id":1}"#
                    } else {
                        ""
                    },
                );
                let keeper = HttpKeeper::new(&base);
                let exec = ExecId {
                    expected_state: state.clone(),
                    ..exec()
                };
                match verb {
                    "report_attempt" => keeper.report_attempt(&report(), &exec).unwrap(),
                    "park" => keeper.park(&park(), &exec).unwrap(),
                    "complete" => keeper.complete("c-1", "sha", &exec).unwrap(),
                    "ask_question" => {
                        keeper.ask_question("c-1", &question(), &exec).unwrap();
                    }
                    _ => keeper.alarm(&alarm(), &exec).unwrap(),
                }
                let body = body_of(&server.join().unwrap());
                assert_eq!(body["workflow_id"], "wf-1");
                assert_eq!(body["run_id"], "run-1");
                assert_eq!(body["attempt"], 3);
                if verb == "alarm" || verb == "report_attempt" || state.is_none() {
                    assert!(body.get("expected_state").is_none());
                } else {
                    assert_eq!(body["expected_state"], "running");
                }
            }
        }
    }
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
        assert!(c.smoke_checks.is_empty());
        assert!(c.exports.is_empty());
        assert!(c.heldout_cmd.is_empty());
        assert!(!c.risk_tagged);
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
            attempt: None,
        }
    }

    /// A fully known execution identity, as an activity with the whole
    /// context and a card attempt in its input would build one.
    fn exec() -> ExecId {
        ExecId {
            expected_state: None,
            workflow_id: Some("wf-1".into()),
            run_id: Some("run-1".into()),
            attempt: Some(3),
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
        keeper.report_attempt(&r, &ExecId::default()).unwrap();
        assert_eq!(keeper.attempts(), vec![r.clone()]);
        assert_eq!(keeper.attempt_execs(), vec![ExecId::default()]);

        let p = park();
        keeper.park(&p, &ExecId::default()).unwrap();
        assert_eq!(keeper.parked(), vec![p.clone()]);

        assert_eq!(keeper.completed(), Vec::<(String, String)>::new());
        keeper.complete("c-1", "sha-1", &ExecId::default()).unwrap();
        assert_eq!(
            keeper.completed(),
            vec![("c-1".to_string(), "sha-1".to_string())]
        );

        let a = alarm();
        keeper.alarm(&a, &ExecId::default()).unwrap();
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

    /// A server that answers `n` requests by routing each one through `route`,
    /// and yields every request it saw, in order (G3-2: an outage on one verb
    /// while another still works).
    fn serve_n(
        n: usize,
        route: impl Fn(&Captured) -> (u16, String) + Send + 'static,
    ) -> (String, JoinHandle<Vec<Captured>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
        let addr = listener.local_addr().expect("read local addr");
        let handle = std::thread::spawn(move || {
            let mut seen = Vec::new();
            for _ in 0..n {
                let (mut stream, _) = listener.accept().expect("accept request");
                let captured = read_request(&mut stream);
                let (status, body) = route(&captured);
                let mut response = format!("HTTP/1.1 {status} Routed\r\n");
                if status != 204 {
                    response.push_str(&format!("Content-Length: {}\r\n", body.len()));
                }
                response.push_str("Content-Type: application/json\r\nConnection: close\r\n\r\n");
                response.push_str(&body);
                stream
                    .write_all(response.as_bytes())
                    .expect("write response");
                stream.flush().expect("flush response");
                seen.push(captured);
            }
            seen
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
            .report_attempt(&report(), &ExecId::default())
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
            .report_attempt(&report(), &ExecId::default())
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

        keeper.report_attempt(&r, &ExecId::default()).unwrap();

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

        let err = keeper
            .report_attempt(&report(), &ExecId::default())
            .unwrap_err();
        assert_eq!(err, KeeperError::Status(500));
        server.join().expect("server thread");
    }

    #[test]
    fn http_remaining_endpoints() {
        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base)
            .park(&park(), &ExecId::default())
            .unwrap();
        assert_eq!(server.join().unwrap().path, "/v1/park");

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base)
            .complete("c-1", "sha-1", &ExecId::default())
            .unwrap();
        let captured = server.join().unwrap();
        assert_eq!(captured.path, "/v1/complete");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&captured.body).unwrap(),
            serde_json::json!({ "card": "c-1", "merged_sha": "sha-1" })
        );

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base)
            .alarm(&alarm(), &ExecId::default())
            .unwrap();
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
            .ask_question("c-1", &question(), &ExecId::default())
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
        assert_eq!(
            k.ask_question("c-1", &question(), &ExecId::default())
                .unwrap(),
            1
        );
        assert_eq!(
            k.ask_question("c-1", &question(), &ExecId::default())
                .unwrap(),
            2
        );
        k.record_question_timeout(2).unwrap();
        k.set_timeout_refused(true);
        assert_eq!(k.record_question_timeout(1), Err(KeeperError::Status(403)));
        assert_eq!(k.questions().len(), 2);
        assert_eq!(k.question_timeouts(), vec![2, 1]);
    }

    /// The captured request body as JSON.
    fn body_of(captured: &Captured) -> serde_json::Value {
        serde_json::from_str(&captured.body).expect("captured body is JSON")
    }

    /// None of the three exec keys is present in the captured body.
    fn assert_no_exec_keys(captured: &Captured) {
        let body = body_of(captured);
        assert!(body.get("workflow_id").is_none(), "{body}");
        assert!(body.get("run_id").is_none(), "{body}");
        assert!(body.get("attempt").is_none(), "{body}");
    }

    /// A fully known exec identity tags all four card-state bodies with
    /// `workflow_id`, `run_id` and `attempt` beside the legacy fields.
    #[test]
    fn http_card_calls_send_the_exec_keys_when_all_three_are_known() {
        let exec = exec();

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base).park(&park(), &exec).unwrap();
        let body = body_of(&server.join().unwrap());
        assert_eq!(
            body,
            serde_json::json!({
                "card": "c-1", "tier": "free", "exit_class": "check_fail",
                "check_tail": "boom",
                "workflow_id": "wf-1", "run_id": "run-1", "attempt": 3,
            })
        );

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base)
            .complete("c-1", "sha-1", &exec)
            .unwrap();
        let body = body_of(&server.join().unwrap());
        assert_eq!(body["workflow_id"], "wf-1");
        assert_eq!(body["run_id"], "run-1");
        assert_eq!(body["attempt"], 3);
        assert_eq!(body["card"], "c-1");
        assert_eq!(body["merged_sha"], "sha-1");

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base).alarm(&alarm(), &exec).unwrap();
        let body = body_of(&server.join().unwrap());
        assert_eq!(body["workflow_id"], "wf-1");
        assert_eq!(body["run_id"], "run-1");
        assert_eq!(body["attempt"], 3);
        assert_eq!(body["card"], "c-1");
        assert_eq!(body["reason"], "stuck");

        let (base, server) = serve_once(200, "OK", r#"{"id":9}"#);
        HttpKeeper::new(&base)
            .ask_question("c-1", &question(), &exec)
            .unwrap();
        let body = body_of(&server.join().unwrap());
        assert_eq!(body["workflow_id"], "wf-1");
        assert_eq!(body["run_id"], "run-1");
        assert_eq!(body["attempt"], 3);
        assert_eq!(body["key"], "c-1");
    }

    /// All three exec fields or none: a default or partial identity sends a
    /// legacy body without any of the three keys, on every one of the four
    /// verbs.
    #[test]
    fn http_card_calls_omit_the_exec_keys_unless_all_three_are_known() {
        let only_workflow = ExecId {
            expected_state: None,
            workflow_id: Some("wf-1".into()),
            run_id: None,
            attempt: None,
        };
        let no_attempt = ExecId {
            expected_state: None,
            workflow_id: Some("wf-1".into()),
            run_id: Some("run-1".into()),
            attempt: None,
        };
        let only_attempt = ExecId {
            expected_state: None,
            workflow_id: None,
            run_id: None,
            attempt: Some(3),
        };

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base)
            .park(&park(), &ExecId::default())
            .unwrap();
        assert_no_exec_keys(&server.join().unwrap());

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base)
            .complete("c-1", "sha-1", &only_workflow)
            .unwrap();
        assert_no_exec_keys(&server.join().unwrap());

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base).alarm(&alarm(), &no_attempt).unwrap();
        assert_no_exec_keys(&server.join().unwrap());

        let (base, server) = serve_once(200, "OK", r#"{"id":9}"#);
        HttpKeeper::new(&base)
            .ask_question("c-1", &question(), &only_attempt)
            .unwrap();
        assert_no_exec_keys(&server.join().unwrap());
    }

    /// JSON recorded before `attempt` existed still decodes: the field
    /// defaults to `None` instead of failing the read. The field is omitted
    /// only when unset; whenever an attempt is known, the body carries
    /// workflow_id/run_id/attempt. An old keeper rejects these as unknown
    /// fields, so deploy the keeper first, then the worker.
    #[test]
    fn old_wire_json_without_attempt_still_deserializes() {
        let alarm: Alarm = serde_json::from_str(r#"{"card":"c-1","reason":"stuck"}"#).unwrap();
        assert_eq!(alarm.attempt, None);

        let done: crate::activities::DoneInput =
            serde_json::from_str(r#"{"card":"c-1","merged_sha":"sha-1"}"#).unwrap();
        assert_eq!(done.attempt, None);
        assert_eq!(done.expected_state, None);

        let ask: crate::activities::AskInput =
            serde_json::from_str(r#"{"card":"c-1","question":{"text":"which db?"}}"#).unwrap();
        assert_eq!(ask.attempt, None);
        assert_eq!(ask.expected_state, None);

        assert_eq!(
            serde_json::to_value(&alarm).unwrap(),
            serde_json::json!({"card": "c-1", "reason": "stuck"})
        );
        assert_eq!(
            serde_json::to_value(&done).unwrap(),
            serde_json::json!({"card": "c-1", "merged_sha": "sha-1"})
        );
        assert_eq!(
            serde_json::to_value(&ask).unwrap(),
            serde_json::json!({
                "card": "c-1",
                "question": {
                    "text": "which db?",
                    "default": null,
                    "deadline_s": null,
                    "risk_tags": [],
                },
            })
        );
    }

    /// Exercise each card-state endpoint through HttpKeeper, not the stub.
    #[test]
    fn http_sends_the_exec_of_every_card_state_call_in_order() {
        for (index, verb) in ["park", "complete", "alarm", "ask_question"]
            .iter()
            .enumerate()
        {
            let (base, server) = serve_once(
                if *verb == "ask_question" { 200 } else { 204 },
                "OK",
                if *verb == "ask_question" {
                    r#"{"id":1}"#
                } else {
                    ""
                },
            );
            let keeper = HttpKeeper::new(&base);
            let exec = ExecId {
                attempt: Some(index as u32 + 1),
                ..exec()
            };
            match *verb {
                "park" => keeper.park(&park(), &exec).unwrap(),
                "complete" => keeper.complete("c-1", "sha", &exec).unwrap(),
                "alarm" => keeper.alarm(&alarm(), &exec).unwrap(),
                _ => {
                    keeper.ask_question("c-1", &question(), &exec).unwrap();
                }
            }
            let body = body_of(&server.join().unwrap());
            assert_eq!(body["workflow_id"], "wf-1");
            assert_eq!(body["run_id"], "run-1");
            assert_eq!(body["attempt"], index as u32 + 1);
        }
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
