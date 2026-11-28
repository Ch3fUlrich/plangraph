//! Loopback host for a self-contained page export and its single MCP settings tool.
use rmcp::model::CallToolRequestParams;
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
};

pub(crate) fn run(args: Vec<String>) -> Result<(), String> {
    if args.len() != 6 || args[0] != "--page" || args[2] != "--repo" || args[4] != "--port" {
        return Err("settings-web --page FILE --repo DIRECTORY --port PORT".into());
    }
    let page = PathBuf::from(&args[1])
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let repo = PathBuf::from(&args[3])
        .canonicalize()
        .map_err(|e| e.to_string())?;
    if !page.is_file() || !repo.is_dir() {
        return Err("expected page file and repository directory".into());
    }
    let port: u16 = args[5].parse().map_err(|_| "invalid port")?;
    let listener =
        TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port)).map_err(|e| e.to_string())?;
    let host = listener
        .local_addr()
        .map_err(|e| e.to_string())?
        .to_string();
    let state = crate::state::resolve_state(None, |key| std::env::var_os(key))?;
    eprintln!("Settings page: http://{host}/");
    for stream in listener.incoming() {
        let mut stream = stream.map_err(|e| e.to_string())?;
        if let Err(error) = handle(&mut stream, &host, &page, &repo, &state) {
            let _ = reply(&mut stream, 400, "text/plain", error.as_bytes());
        }
    }
    Ok(())
}

fn reply(out: &mut impl Write, status: u16, content_type: &str, body: &[u8]) -> Result<(), String> {
    write!(out, "HTTP/1.1 {status} Result\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\nX-Content-Type-Options: nosniff\r\n\r\n", body.len())
        .and_then(|()| out.write_all(body)).map_err(|e| e.to_string())
}
fn handle(
    stream: &mut TcpStream,
    host: &str,
    page: &Path,
    repo: &Path,
    state: &Path,
) -> Result<(), String> {
    stream
        .set_read_timeout(Some(std::time::Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;
    let mut reader = BufReader::new(&mut *stream);
    let mut headers = Vec::new();
    loop {
        let start = headers.len();
        reader
            .by_ref()
            .take((8193 - start) as u64)
            .read_until(b'\n', &mut headers)
            .map_err(|e| e.to_string())?;
        if headers.len() > 8192 || headers.len() == start {
            return Err("invalid headers".into());
        }
        if &headers[start..] == b"\r\n" {
            break;
        }
    }
    let text = std::str::from_utf8(&headers).map_err(|_| "invalid headers")?;
    let mut lines = text.split("\r\n");
    let request = lines.next().ok_or("missing request")?;
    let mut fields = std::collections::HashMap::new();
    for line in lines.filter(|line| !line.is_empty()) {
        let (key, value) = line.split_once(':').ok_or("invalid header")?;
        if fields
            .insert(key.to_ascii_lowercase(), value.trim())
            .is_some()
        {
            return Err("duplicate header".into());
        }
    }
    let size: usize = fields
        .get("content-length")
        .copied()
        .unwrap_or("0")
        .parse()
        .map_err(|_| "invalid content length")?;
    if size > 4096 {
        return Err("request too large".into());
    }
    let mut bytes = vec![0; size];
    reader.read_exact(&mut bytes).map_err(|e| e.to_string())?;
    let (status, content_type, body) = if fields.get("host") != Some(&host) {
        (403, "text/plain", b"invalid host".to_vec())
    } else if request == "GET / HTTP/1.1" {
        (
            200,
            "text/html; charset=utf-8",
            std::fs::read(page).map_err(|e| e.to_string())?,
        )
    } else if request == "POST /mcp HTTP/1.1" {
        if fields.get("origin").copied() != Some(format!("http://{host}").as_str())
            || fields.get("content-type").copied() != Some("application/json")
            || fields.contains_key("transfer-encoding")
        {
            (403, "text/plain", b"same-origin JSON required".to_vec())
        } else {
            (
                200,
                "application/json",
                rpc(&bytes, repo, state).to_string().into_bytes(),
            )
        }
    } else {
        (404, "text/plain", b"not found".to_vec())
    };
    drop(reader);
    reply(stream, status, content_type, &body)
}
fn rpc(bytes: &[u8], repo: &Path, state: &Path) -> Value {
    let Ok(mut request) = serde_json::from_slice::<Value>(bytes) else {
        return json!({"error":{"message":"invalid JSON"}});
    };
    let id = request["id"].clone();
    let result = (|| {
        if request["jsonrpc"] != "2.0"
            || request["method"] != "tools/call"
            || request["params"]["name"] != "plangraph_work_mode"
        {
            return Err("only the work-mode tool is available");
        }
        let args = request["params"]["arguments"]
            .as_object_mut()
            .ok_or("arguments must be an object")?;
        args.insert("repo".into(), json!(repo));
        let params: CallToolRequestParams = serde_json::from_value(request["params"].clone())
            .map_err(|_| "invalid tool request")?;
        crate::tools::dispatch(&params, state, "local", None)
            .map_err(|_| "work mode request failed")
    })();
    match result {
        Ok(result) => json!({"jsonrpc":"2.0","id":id,"result":result}),
        Err(message) => json!({"jsonrpc":"2.0","id":id,"error":{"message":message}}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn settings_web_serves_page_and_fences_origin_host_and_tool() {
        let dir = tempfile::tempdir().unwrap();
        let page = dir.path().join("page.html");
        std::fs::write(&page, "exported page").unwrap();
        let exchange = |method: &str, origin: &str, valid_host: bool, body: &str| {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let host = listener.local_addr().unwrap().to_string();
            std::thread::scope(|scope| {
                let host_ref = &host;
                let page_ref = &page;
                let root = dir.path();
                scope.spawn(move || {
                    let (mut stream, _) = listener.accept().unwrap();
                    handle(&mut stream, host_ref, page_ref, root, root).unwrap();
                });
                let mut client = TcpStream::connect(&host).unwrap();
                let origin = if origin == "same" {
                    format!("http://{host}")
                } else {
                    origin.into()
                };
                write!(client, "{method} HTTP/1.1\r\nHost: {}\r\nOrigin: {origin}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", if valid_host { &host } else { "other.invalid" }, body.len()).unwrap();
                let mut response = String::new();
                client.read_to_string(&mut response).unwrap();
                response
            })
        };
        assert!(exchange("GET /", "", true, "").ends_with("exported page"));
        for origin in ["", "null", "https://other.invalid"] {
            assert!(exchange("POST /mcp", origin, true, "{}").starts_with("HTTP/1.1 403"));
        }
        assert!(exchange("POST /mcp", "same", false, "{}").starts_with("HTTP/1.1 403"));
        assert!(exchange("GET /../page.html", "same", true, "").starts_with("HTTP/1.1 404"));
        let request = r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"plangraph_agent_spawn","arguments":{}}}"#;
        assert!(exchange("POST /mcp", "same", true, request).contains("only the work-mode tool"));
        assert!(!dir.path().join("work-mode.json").exists());
    }
}
