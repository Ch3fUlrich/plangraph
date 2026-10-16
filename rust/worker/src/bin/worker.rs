//! Worker entry point.
//!
//! Wiring is env-driven so the same binary runs against a local stub or the
//! real keeper without a rebuild: `PG_TEMPORAL_ADDR`, `PG_NAMESPACE`,
//! `PG_KEEPER_URL`, `PG_KEEPER_TOKEN`, `PG_HARNESS_BIN`.
//!
//! The keeper is required: with `PG_KEEPER_URL` unset or empty the worker exits
//! with an error rather than running against an in-memory stand-in that would
//! claim nothing and silently drop every report, park and merge lock. Local
//! tests and examples opt in to the stand-in explicitly with `PG_KEEPER_STUB=1`
//! (ignored when `PG_KEEPER_URL` is set); it is local-only and must never be
//! set on a live deployment. With a keeper URL, `PG_KEEPER_TOKEN` (the worker
//! role bearer token) must be set and non-empty.
//!
//! `PG_HARNESS_VERSIONS` (`name=version,...`) pins the agent CLIs. Unset is only
//! a startup warning, not an error, so live deployments MUST set it: without it
//! a CLI upgrade changes agent behaviour unnoticed.
//!
//! `PG_OPENCODE_SERVER_URL` is a bare `http(s)://host[:port]` URL: userinfo
//! (`user:pass@`) and other schemes are rejected at startup. Tokens go through
//! the harness env, never the URL or argv (argv is world-readable).
//!
//! The merge-lock fence high-water mark (`mergelock`) relies on the keeper
//! issuing strictly monotonic fence tokens: a keeper that reuses or rewinds a
//! token would let a stale holder pass the fence. Worker deployment versioning is opt-in via
//! [`pg_worker::buildid::from_env`], which stays off unless a valid build id is
//! available (see that module for the switch).

use std::str::FromStr;
use std::sync::Arc;

use pg_worker::activities::{CardActivities, Deps, TASK_QUEUE};
use pg_worker::buildid;
use pg_worker::harness::{self, ClaudeCode, HarnessRegistry, Opencode, OpencodeServer};
use pg_worker::keeper::{HttpKeeper, Keeper, StubKeeper};
use pg_worker::workflow::Card;
use pg_worker_core::config::Config;
use pg_worker_core::exit_class::Patterns;
use temporalio_client::{Client, ClientOptions, Connection, ConnectionOptions, Url};
use temporalio_common::worker::{
    VersioningBehavior, WorkerDeploymentOptions, WorkerDeploymentVersion,
};
use temporalio_sdk::{Runtime, Worker, WorkerOptions};

/// Env var `name`, or `default` when unset or empty.
///
/// Treating an empty value as unset keeps `VAR= docker run ...` from quietly
/// producing a blank namespace or address.
fn env_or(name: &str, default: &str) -> String {
    match std::env::var(name) {
        Ok(v) if !v.is_empty() => v,
        _ => default.to_string(),
    }
}

/// Opencode is always registered. Claude and the opencode server are optional
/// legs: they exist only when their binary is on `PATH` / their URL is set, so
/// a worker without them still runs and a card naming them becomes a planner
/// defect instead of a crash loop.
fn build_registry() -> HarnessRegistry {
    let mut reg = HarnessRegistry::new();
    let opencode_bin = env_or("PG_HARNESS_BIN", "opencode");
    reg.register(
        "opencode",
        Arc::new(Opencode {
            bin: opencode_bin.clone(),
        }),
    );
    let claude_bin = env_or("PG_CLAUDE_BIN", "claude");
    if binary_on_path(&claude_bin) {
        reg.register("claude", Arc::new(ClaudeCode { bin: claude_bin }));
    }
    if let Ok(mut server) = OpencodeServer::from_env() {
        server.bin = opencode_bin;
        reg.register("opencode-server", Arc::new(server));
    }
    reg
}

/// True when `bin` is an existing path or resolves through `PATH`.
fn binary_on_path(bin: &str) -> bool {
    let p = std::path::Path::new(bin);
    if p.components().count() > 1 {
        return p.is_file();
    }
    std::env::var_os("PATH")
        .is_some_and(|paths| std::env::split_paths(&paths).any(|d| d.join(bin).is_file()))
}

/// Choose the keeper: the HTTP one when `url` is set and non-empty; otherwise
/// the in-memory stub only if `stub` is exactly `1`; otherwise an error.
///
/// The keeper authenticates every verb with the worker role's bearer token, so
/// an HTTP keeper requires `PG_KEEPER_TOKEN` to be set and non-empty: a blank
/// token would only surface later as an opaque 401 on the first report.
fn select_keeper(
    url: Option<&str>,
    stub: Option<&str>,
    token: Option<&str>,
) -> anyhow::Result<Arc<dyn Keeper>> {
    match url {
        Some(u) if !u.is_empty() => match token {
            Some(t) if !t.is_empty() => Ok(Arc::new(HttpKeeper::new(u).with_token(t))),
            _ => anyhow::bail!(
                "PG_KEEPER_TOKEN is unset or empty; the keeper needs the worker role token"
            ),
        },
        _ if stub == Some("1") => {
            eprintln!("warning: PG_KEEPER_STUB=1; using the in-memory stub keeper (tests only)");
            Ok(Arc::new(StubKeeper::default()))
        }
        _ => anyhow::bail!(
            "PG_KEEPER_URL is not set; refusing to start without a keeper \
             (set PG_KEEPER_STUB=1 only for local tests and examples)"
        ),
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let addr = env_or("PG_TEMPORAL_ADDR", "http://127.0.0.1:7233");
    let namespace = env_or("PG_NAMESPACE", "default");

    let keeper = select_keeper(
        std::env::var("PG_KEEPER_URL").ok().as_deref(),
        std::env::var("PG_KEEPER_STUB").ok().as_deref(),
        std::env::var("PG_KEEPER_TOKEN").ok().as_deref(),
    )?;
    let harnesses = build_registry();
    println!("harnesses: {}", harnesses.names().join(", "));
    // Fail closed on a harness version drift; unset means no pin.
    let pins = std::env::var(harness::VERSIONS_ENV).ok();
    match harness::check_version_pins(&harnesses, pins.as_deref()) {
        Ok(true) => println!("harness versions pinned and verified"),
        Ok(false) => eprintln!(
            "warning: {} is unset; harness versions are not pinned",
            harness::VERSIONS_ENV
        ),
        Err(e) => anyhow::bail!("harness version pin: {e}"),
    }
    let deps = Deps {
        keeper,
        harnesses,
        cfg: Config::default(),
        patterns: Patterns::default(),
    };

    let runtime = Runtime::from_current_tokio(Default::default())?;
    let tls = pg_worker::tls::tls_from_env().map_err(|e| anyhow::anyhow!(e))?;
    pg_worker::tls::require_https(&addr, &tls).map_err(|e| anyhow::anyhow!(e))?;
    let conn = Connection::connect(
        ConnectionOptions::new(Url::from_str(&addr)?)
            .maybe_tls_options(tls)
            .build(),
    )
    .await?;
    let client = Client::new(conn, ClientOptions::new(namespace).build())?;

    let builder = WorkerOptions::new(TASK_QUEUE)
        .register_workflow::<Card>()?
        .register_activities(CardActivities::new(deps));
    let opts = match buildid::from_env() {
        Some((deployment, build)) => {
            println!("worker versioning on: deployment={deployment} build_id={build}");
            let version = WorkerDeploymentVersion::builder()
                .deployment_name(deployment)
                .build_id(build)
                .build();
            builder
                .deployment_options(
                    WorkerDeploymentOptions::new(version)
                        .use_worker_versioning(true)
                        .default_versioning_behavior(VersioningBehavior::Pinned)
                        .build(),
                )
                .build()
        }
        None => builder.build(),
    };

    let mut worker = Worker::new(&runtime, client, opts)?;
    println!("worker up on task queue {TASK_QUEUE}");

    // `run` borrows the worker mutably, so the shutdown handle is taken first;
    // ctrl-c then asks the SDK to drain and we keep awaiting `run` until it
    // resolves, rather than dropping it mid-poll.
    let shutdown = worker.shutdown_handle();
    let run = worker.run();
    tokio::pin!(run);
    let mut draining = false;
    tokio::select! {
        res = &mut run => res?,
        res = tokio::signal::ctrl_c() => {
            match res {
                Ok(()) => println!("ctrl-c received: shutting down"),
                Err(e) => eprintln!("ctrl-c handler failed: {e}; shutting down"),
            }
            shutdown();
            draining = true;
        }
    }
    if draining {
        run.await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::select_keeper;

    #[test]
    fn missing_keeper_url_fails_closed_unless_the_stub_is_explicit() {
        assert!(select_keeper(None, None, None).is_err());
        assert!(select_keeper(Some(""), None, None).is_err());
        // Only an exact `1` opts in.
        assert!(select_keeper(None, Some("0"), None).is_err());
        assert!(select_keeper(None, Some("true"), None).is_err());
        assert!(select_keeper(None, Some("1"), None).is_ok());
        assert!(select_keeper(Some("http://127.0.0.1:1"), None, Some("t")).is_ok());
    }

    #[test]
    fn http_keeper_requires_a_non_empty_token() {
        let u = Some("http://127.0.0.1:1");
        assert!(select_keeper(u, None, None).is_err());
        assert!(select_keeper(u, None, Some("")).is_err());
        assert!(select_keeper(u, Some("1"), Some("")).is_err());
    }
}
