//! `pg-spawnd serve`: the local MCP server (`tools/list` contract plus
//! `plangraph_agent_health`; every other listed tool is a stub for R6a-3).

use rmcp::model::{
    CallToolRequestParams, CallToolResult, Implementation, InitializeResult, ListToolsResult,
    PaginatedRequestParams, ServerCapabilities, ServerInfo,
};
use rmcp::service::{MaybeSendFuture, RequestContext};
use rmcp::{ErrorData as McpError, RoleServer, ServerHandler};
use std::future::Future;

pub(crate) struct SpawndServer;

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
        std::future::ready(crate::tools::call_tool(&request))
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
pub(crate) fn run(socket: Option<String>) -> Result<(), String> {
    let path = crate::transport_unix::resolve_socket_path(socket.as_deref())?;
    runtime()?.block_on(serve_unix(&path))
}

#[cfg(unix)]
async fn serve_unix(path: &std::path::Path) -> Result<(), String> {
    let listener = crate::transport_unix::bind_listener(path)?;
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let listener = tokio::net::UnixListener::from_std(listener).map_err(|e| e.to_string())?;
    loop {
        let (stream, _peer) = listener
            .accept()
            .await
            .map_err(|e| format!("accept failed: {e}"))?;
        // Handshake failures (liveness probes that connect and close) are not
        // fatal to the server.
        spawn_session(stream);
    }
}

/// Serve MCP connections on the per-user named pipe until the process is killed.
#[cfg(windows)]
pub(crate) fn run(socket: Option<String>) -> Result<(), String> {
    if socket.is_some() {
        return Err("--socket is unsupported; use the per-user pg-spawnd pipe".into());
    }
    runtime()?.block_on(serve_windows())
}

#[cfg(windows)]
async fn serve_windows() -> Result<(), String> {
    let mut first_instance = true;
    loop {
        let server = crate::transport_windows::create_pipe_server(first_instance)?;
        first_instance = false;
        if server.connect().await.is_err() {
            eprintln!("pipe connection closed before session started");
            continue;
        }
        spawn_session(server);
    }
}

fn spawn_session<S>(stream: S)
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    tokio::spawn(async move {
        match tokio::time::timeout(
            std::time::Duration::from_secs(10),
            rmcp::serve_server(SpawndServer, stream),
        )
        .await
        {
            Ok(Ok(service)) => {
                let _ = service.waiting().await;
            }
            _ => eprintln!("client initialization failed or timed out"),
        }
    });
}
