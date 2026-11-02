//! `pg-spawnd shim`: bridge stdin/stdout to the local MCP server socket.
//!
//! Bytes are copied verbatim in both directions; the bridge exits 0 when either
//! side closes. Stdio is bridged through blocking reads/writes on the blocking
//! pool so the two copies can proceed concurrently.

use std::collections::VecDeque;
use std::future::Future;
use std::io::{Read, Write};
use std::pin::Pin;
use std::task::{Context, Poll};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

const CONNECT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(2);

fn runtime() -> Result<tokio::runtime::Runtime, String> {
    tokio::runtime::Builder::new_current_thread()
        .enable_io()
        .enable_time()
        .build()
        .map_err(|e| e.to_string())
}

#[cfg(unix)]
pub(crate) fn run(socket: Option<String>) -> Result<(), String> {
    let path = crate::transport_unix::resolve_socket_path(socket.as_deref())?;
    runtime()?.block_on(async {
        let stream = tokio::time::timeout(CONNECT_TIMEOUT, crate::transport_unix::connect(&path))
            .await
            .map_err(|_| format!("timed out connecting to {}", path.display()))??;
        bridge(stream).await
    })
}

#[cfg(windows)]
pub(crate) fn run(_socket: Option<String>) -> Result<(), String> {
    runtime()?.block_on(async {
        let stream = tokio::time::timeout(CONNECT_TIMEOUT, crate::transport_windows::connect())
            .await
            .map_err(|_| "timed out connecting to the pg-spawnd pipe".to_string())??;
        bridge(stream).await
    })
}

/// Copy bytes both ways until either direction hits EOF.
async fn bridge<S>(stream: S) -> Result<(), String>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let (mut socket_read, mut socket_write) = tokio::io::split(stream);
    let mut stdin = BlockingStdin::default();
    let mut stdout = BlockingStdout::default();
    tokio::select! {
        result = tokio::io::copy(&mut stdin, &mut socket_write) => {
            result.map(|_| ()).map_err(|e| format!("stdin copy failed: {e}"))
        }
        result = tokio::io::copy(&mut socket_read, &mut stdout) => {
            result.map(|_| ()).map_err(|e| format!("socket copy failed: {e}"))
        }
    }
}

/// `AsyncRead` over stdin: each fill runs a blocking read on the pool.
#[derive(Default)]
struct BlockingStdin {
    pending: Option<tokio::task::JoinHandle<std::io::Result<Vec<u8>>>>,
    leftover: VecDeque<u8>,
}

impl AsyncRead for BlockingStdin {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let this = &mut *self;
        let take = this.leftover.len().min(buf.remaining());
        if take > 0 {
            let chunk: Vec<u8> = this.leftover.drain(..take).collect();
            buf.put_slice(&chunk);
            return Poll::Ready(Ok(()));
        }
        if this.pending.is_none() {
            this.pending = Some(tokio::task::spawn_blocking(|| {
                let mut chunk = vec![0u8; 8192];
                let read = Read::read(&mut std::io::stdin(), &mut chunk)?;
                chunk.truncate(read);
                Ok(chunk)
            }));
        }
        match Pin::new(this.pending.as_mut().expect("pending read")).poll(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(result) => {
                this.pending = None;
                let chunk = match result {
                    Err(join) => return Poll::Ready(Err(std::io::Error::other(join))),
                    Ok(Err(err)) => return Poll::Ready(Err(err)),
                    Ok(Ok(chunk)) => chunk,
                };
                let take = chunk.len().min(buf.remaining());
                buf.put_slice(&chunk[..take]);
                this.leftover.extend(&chunk[take..]);
                Poll::Ready(Ok(()))
            }
        }
    }
}

/// `AsyncWrite` over stdout: one blocking write is in flight at a time.
#[derive(Default)]
struct BlockingStdout {
    pending: Option<tokio::task::JoinHandle<std::io::Result<()>>>,
}

impl BlockingStdout {
    fn poll_write_done(&mut self, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        match self.pending.as_mut() {
            None => Poll::Ready(Ok(())),
            Some(handle) => match Pin::new(handle).poll(cx) {
                Poll::Pending => Poll::Pending,
                Poll::Ready(result) => {
                    self.pending = None;
                    match result {
                        Err(join) => Poll::Ready(Err(std::io::Error::other(join))),
                        Ok(inner) => Poll::Ready(inner),
                    }
                }
            },
        }
    }
}

impl AsyncWrite for BlockingStdout {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        let this = &mut *self;
        if this.pending.is_none() {
            let data = buf.to_vec();
            this.pending = Some(tokio::task::spawn_blocking(move || {
                Write::write_all(&mut std::io::stdout(), &data)
            }));
        }
        match this.poll_write_done(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(Err(err)) => Poll::Ready(Err(err)),
            Poll::Ready(Ok(())) => Poll::Ready(Ok(buf.len())),
        }
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        self.get_mut().poll_write_done(cx)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        self.get_mut().poll_write_done(cx)
    }
}
