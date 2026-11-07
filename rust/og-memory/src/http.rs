//! Omnigraph 0.13 HTTP boundary; no retries or redirect following.
//! Companion publication uses a conditional receipt-state follow-up.
use crate::{credentials::read_token, io::Error};
use serde::Serialize;
use serde_json::Value;
use std::{path::Path, time::Duration};

#[path = "http_publish.rs"]
mod publish;
#[path = "http_recall.rs"]
mod recall;

pub struct Http {
    agent: ureq::Agent,
    base: String,
    token: String,
}

#[derive(Serialize)]
pub struct Request<'a, P: Serialize> {
    pub query: &'a str,
    pub params: P,
}

#[derive(Serialize)]
struct PinnedRequest<'a> {
    query: &'a str,
    params: Value,
    snapshot: &'a str,
}

impl Http {
    fn post_snapshot(&self, query: &str, project: &str, snapshot: &str) -> Result<Value, Error> {
        self.post(
            "query",
            &PinnedRequest {
                query,
                params: serde_json::json!({"project": project}),
                snapshot,
            },
            None,
        )
    }
    /// `base` includes the graph route. Only TLS or loopback HTTP is accepted.
    pub fn new(base: &str, token_file: &Path, timeout: Duration) -> Result<Self, Error> {
        let uri: ureq::http::Uri = base.parse().map_err(|_| Error::Outage)?;
        if uri.authority().is_none_or(|a| a.as_str().contains('@'))
            || uri.query().is_some()
            || base.contains('#')
            || !(uri.scheme_str() == Some("https")
                || (uri.scheme_str() == Some("http") && uri.host().is_some_and(loopback)))
            || timeout.is_zero()
        {
            return Err(Error::Outage);
        }
        Ok(Self {
            agent: ureq::Agent::config_builder()
                // ureq 3.5.1 defaults to Proxy::try_from_env(); never forward credentials.
                .proxy(None)
                .max_redirects(0)
                .timeout_global(Some(timeout))
                .build()
                .into(),
            base: base.trim_end_matches('/').into(),
            token: read_token(token_file)?,
        })
    }

    pub fn query<P: Serialize>(&self, request: &Request<'_, P>) -> Result<Value, Error> {
        self.post("query", request, None)
    }

    /// A lost or malformed write response is ambiguous, never a retry instruction.
    pub fn mutate<P: Serialize>(
        &self,
        request: &Request<'_, P>,
        if_graph_commit: &str,
    ) -> Result<Value, Error> {
        if if_graph_commit.is_empty() || !if_graph_commit.bytes().all(|b| b.is_ascii_graphic()) {
            return Err(Error::Conflict);
        }
        self.post("mutate/if-graph-commit", request, Some(if_graph_commit))
    }

    fn post<P: Serialize>(
        &self,
        route: &str,
        body: &P,
        expected: Option<&str>,
    ) -> Result<Value, Error> {
        let failure = || {
            if expected.is_some() {
                Error::Unknown
            } else {
                Error::Outage
            }
        };
        // Serialize before dispatch: serialization failure cannot have committed.
        let body = serde_json::to_vec(body).map_err(|_| Error::Outage)?;
        let mut request = self
            .agent
            .post(format!("{}/{route}", self.base))
            .header("omnigraph-http-api", "0.13")
            .header("Authorization", format!("Bearer {}", self.token))
            .header("Content-Type", "application/json");
        if let Some(commit) = expected {
            request = request.header("Omnigraph-If-Graph-Commit", commit);
        }
        let mut response = request.send(body.as_slice()).map_err(|e| match e {
            ureq::Error::StatusCode(412) => Error::Conflict,
            _ => failure(),
        })?;
        if !response.status().is_success() {
            return Err(failure());
        }
        response
            .body_mut()
            .with_config()
            .limit(1024 * 1024)
            .read_json()
            .map_err(|_| failure())
    }
}

fn loopback(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost")
        || host
            .trim_start_matches('[')
            .trim_end_matches(']')
            .parse::<std::net::IpAddr>()
            .is_ok_and(|address| address.is_loopback())
}

#[cfg(all(test, unix))]
#[path = "http_tests.rs"]
mod tests;

#[cfg(all(test, unix))]
#[path = "http_recall_tests.rs"]
mod recall_tests;
