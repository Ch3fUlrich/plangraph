//! `pg-spawnd serve`: local MCP server with health and shared record readers.
//! One supervisor is shared across every connection.

use pg_worker_core::run::supervisor::{Supervisor, SupervisorCfg};
use rmcp::model::{
    CallToolRequestParams, CallToolResult, Implementation, InitializeResult, ListToolsResult,
    PaginatedRequestParams, ServerCapabilities, ServerInfo,
};
use rmcp::service::{MaybeSendFuture, RequestContext};
use rmcp::{ErrorData as McpError, RoleServer, ServerHandler};
use std::future::Future;
use std::sync::Arc;

pub(crate) struct SpawndServer {
    state: std::path::PathBuf,
    host: String,
    runs: Arc<crate::runs::Runs>,
}

impl ServerHandler for SpawndServer {
    fn get_info(&self) -> ServerInfo {
        InitializeResult::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("pg-spawnd", env!("CARGO_PKG_VERSION")))
    }

    fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<ListToolsResult, McpError>> + MaybeSendFuture + '_ {
        std::future::ready(Ok(crate::tools::list_tools()))
    }

    fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> impl Future<Output = Result<CallToolResult, McpError>> + MaybeSendFuture + '_ {
        let state = self.state.clone();
        let host = self.host.clone();
        let runs = self.runs.clone();
        async move {
            tokio::task::spawn_blocking(move || {
                crate::tools::dispatch(&request, &state, &host, Some(&runs))
            })
            .await
            .map_err(|_| McpError::internal_error("tool task failed", None))?
        }
    }
}

fn runtime() -> Result<tokio::runtime::Runtime, String> {
    tokio::runtime::Builder::new_current_thread()
        .enable_io()
        .enable_time()
        .build()
        .map_err(|e| e.to_string())
}

/// Serve MCP connections until the process is killed.
#[cfg(unix)]
pub(crate) fn run(
    socket: Option<String>,
    state: std::path::PathBuf,
    mut cfg: SupervisorCfg,
) -> Result<(), String> {
    cfg.run.state_dir = state.clone();
    let _recovery = crate::reconcile_serve::configured(&state)?;
    let host = cfg.run.host.clone();
    let supervisor = Arc::new(Supervisor::new(cfg).map_err(|e| e.to_string())?);
    let path = crate::transport_unix::resolve_socket_path(socket.as_deref())?;
    runtime()?.block_on(serve_unix(&path, state, supervisor, host))
}

#[cfg(unix)]
async fn serve_unix(
    path: &std::path::Path,
    state: std::path::PathBuf,
    supervisor: Arc<Supervisor>,
    host: String,
) -> Result<(), String> {
    let listener = crate::transport_unix::bind_listener(path)?;
    let runs = Arc::new(crate::runs::Runs::new(supervisor.clone()));
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let listener = tokio::net::UnixListener::from_std(listener).map_err(|e| e.to_string())?;
    loop {
        let (stream, _peer) = match listener.accept().await {
            Ok(pair) => pair,
            Err(e) if retryable_accept(&e) => {
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                continue;
            }
            Err(e) => return Err(format!("accept failed: {e}")),
        };
        // Handshake failures (liveness probes that connect and close) are not
        // fatal to the server.
        spawn_session(stream, state.clone(), host.clone(), runs.clone());
    }
}

/// Serve MCP connections on the per-user named pipe until the process is killed.
#[cfg(windows)]
pub(crate) fn run(
    socket: Option<String>,
    state: std::path::PathBuf,
    mut cfg: SupervisorCfg,
) -> Result<(), String> {
    cfg.run.state_dir = state.clone();
    let _recovery = crate::reconcile_serve::configured(&state)?;
    let host = cfg.run.host.clone();
    let supervisor = Arc::new(Supervisor::new(cfg).map_err(|e| e.to_string())?);
    if socket.is_some() {
        return Err("--socket is unsupported; use the per-user pg-spawnd pipe".into());
    }
    runtime()?.block_on(serve_windows(state, supervisor, host))
}

#[cfg(windows)]
async fn serve_windows(
    state: std::path::PathBuf,
    supervisor: Arc<Supervisor>,
    host: String,
) -> Result<(), String> {
    let mut first_instance = true;
    let runs = Arc::new(crate::runs::Runs::new(supervisor.clone()));
    loop {
        let server = crate::transport_windows::create_pipe_server(first_instance)?;
        first_instance = false;
        if server.connect().await.is_err() {
            continue;
        }
        spawn_session(server, state.clone(), host.clone(), runs.clone());
    }
}

fn spawn_session<S>(
    stream: S,
    state: std::path::PathBuf,
    host: String,
    runs: Arc<crate::runs::Runs>,
) where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    tokio::spawn(async move {
        if let Ok(Ok(service)) = tokio::time::timeout(
            handshake_timeout(),
            rmcp::serve_server(SpawndServer { state, host, runs }, stream),
        )
        .await
        {
            let _ = service.waiting().await;
        }
    });
}

fn handshake_timeout() -> std::time::Duration {
    std::env::var("PG_SPAWND_CONNECT_TIMEOUT_MS")
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .filter(|ms| *ms != u64::MAX)
        .map(std::time::Duration::from_millis)
        .unwrap_or(std::time::Duration::from_secs(10))
}

#[cfg(unix)]
fn retryable_accept(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::Interrupted
            | std::io::ErrorKind::WouldBlock
            | std::io::ErrorKind::ConnectionAborted
    ) || matches!(error.raw_os_error(), Some(23 | 24))
}

#[cfg(test)]
mod tests {
    #[cfg(unix)]
    #[test]
    fn transient_accept_errors_are_retryable() {
        for code in [23, 24] {
            assert!(super::retryable_accept(&std::io::Error::from_raw_os_error(
                code
            )));
        }
        for kind in [
            std::io::ErrorKind::Interrupted,
            std::io::ErrorKind::WouldBlock,
            std::io::ErrorKind::ConnectionAborted,
        ] {
            assert!(super::retryable_accept(&std::io::Error::from(kind)));
        }
        assert!(!super::retryable_accept(&std::io::Error::from(
            std::io::ErrorKind::PermissionDenied
        )));
    }
}
