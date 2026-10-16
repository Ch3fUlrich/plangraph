You have NO tools and no repository access. The full material is inline below. Answer in plain text; never emit tool calls.

HIGH-style security review (TLS/auth change). Rust crates using Temporal SDK temporalio-client =1.1.0 (TlsOptions{server_root_ca_cert,domain,client_tls_options}, ConnectionOptions bon builder with maybe_tls_options). Change: env-driven mTLS (TEMPORAL_TLS_CERT/KEY/CA/SERVER_NAME) for 3 binaries. Unset env = unchanged plain connect. rust/spikes/temporal-spike/src/tls.rs is a byte-identical copy of rust/worker/src/tls.rs (shown once). Test evidence: 7 unit tests pass per crate; openssl s_server -Verify 1 handshake verified the client cert chain; https without TLS env is refused by the SDK.
Vectors (answer each with file:function evidence):
V1 Fail-open: can any env/config combination silently fall back to plaintext or to unauthenticated TLS (partial set, empty values, SERVER_NAME alone, http:// URL with TLS vars)?
V2 Secret handling: can key/cert bytes reach logs, errors, panics, Debug output?
V3 Server verification: is the CA/domain used so the server cert is actually verified (no insecure verifier, no accidental system-roots-only when CA given)?
V4 Error paths: any unwrap/panic reachable from env input; trimming/whitespace of paths; behaviour on non-PEM files.
V5 Wiring: do all three binaries use the helper on the connect path; any other Connection::connect left plain?
V6 Test quality: do tests actually pin the refusal rules; gaming/vacuous asserts?

=== DIFF (git diff, tls.rs of the spike omitted = copy of worker's) ===
diff --git a/rust/TLS.md b/rust/TLS.md
new file mode 100644
index 0000000..5be8c42
--- /dev/null
+++ b/rust/TLS.md
@@ -0,0 +1,17 @@
+# mTLS for the Temporal connection
+
+`rust/worker` and `rust/spikes/temporal-spike` (worker + driver) read the same four env vars
+(`src/tls.rs` in each crate, identical copies):
+
+| Variable | Meaning |
+|---|---|
+| `TEMPORAL_TLS_CERT` | client certificate, PEM file path |
+| `TEMPORAL_TLS_KEY` | client private key, PEM file path |
+| `TEMPORAL_TLS_CA` | CA that signed the server certificate, PEM file path |
+| `TEMPORAL_TLS_SERVER_NAME` | optional: DNS name to verify the server cert against (default: host of the address) |
+
+- None set (empty = unset): plain connect, unchanged.
+- Any of CERT/KEY/CA set: all three are required; a partial set, or SERVER_NAME alone, is refused at startup naming the missing variables. An unreadable file is refused naming the variable and path (never contents).
+- The address must be `https://...` (`PG_TEMPORAL_ADDR` for the worker, `SPIKE_ADDR` for the spike), e.g. the nginx sidecar's mTLS port. nginx does not pin a server_name, so set SERVER_NAME to a SAN of the server cert when connecting by IP.
+
+Checks: `cargo test --lib tls` (env -> options unit tests); `rust/tls-handshake-check.sh` (WSL/Linux: throwaway CA, openssl mTLS server, shows the client cert is presented and verified, and that https without TLS env is refused). The full gRPC round trip against the real frontend is the manual check for TEMPORAL-CHECK on the Temporal host.
diff --git a/rust/spikes/temporal-spike/Cargo.toml b/rust/spikes/temporal-spike/Cargo.toml
index 78bb3f5..69aea52 100644
--- a/rust/spikes/temporal-spike/Cargo.toml
+++ b/rust/spikes/temporal-spike/Cargo.toml
@@ -20,5 +20,8 @@ serde = { version = "1", features = ["derive"] }
 serde_json = "1"
 anyhow = "1"
 
+[dev-dependencies]
+tempfile = "3"
+
 [profile.release]
 lto = "thin"
diff --git a/rust/spikes/temporal-spike/src/bin/driver.rs b/rust/spikes/temporal-spike/src/bin/driver.rs
index 112f951..317657c 100644
--- a/rust/spikes/temporal-spike/src/bin/driver.rs
+++ b/rust/spikes/temporal-spike/src/bin/driver.rs
@@ -198,7 +198,12 @@ async fn all(c: &Client, fixtures: Option<&str>) -> anyhow::Result<()> {
 #[tokio::main]
 async fn main() -> anyhow::Result<()> {
     let addr = std::env::var("SPIKE_ADDR").unwrap_or_else(|_| "http://127.0.0.1:7233".into());
-    let conn = Connection::connect(ConnectionOptions::new(Url::from_str(&addr)?).build()).await?;
+    let conn = Connection::connect(
+        ConnectionOptions::new(Url::from_str(&addr)?)
+            .maybe_tls_options(temporal_spike::tls::tls_from_env().map_err(|e| anyhow::anyhow!(e))?)
+            .build(),
+    )
+    .await?;
     let c = Client::new(conn, ClientOptions::new("default").build())?;
     let args: Vec<String> = std::env::args().skip(1).collect();
     match args
diff --git a/rust/spikes/temporal-spike/src/bin/worker.rs b/rust/spikes/temporal-spike/src/bin/worker.rs
index 531dd4d..54d2ca3 100644
--- a/rust/spikes/temporal-spike/src/bin/worker.rs
+++ b/rust/spikes/temporal-spike/src/bin/worker.rs
@@ -13,7 +13,12 @@ use temporalio_sdk::{Runtime, Worker, WorkerOptions};
 async fn main() -> anyhow::Result<()> {
     let addr = std::env::var("SPIKE_ADDR").unwrap_or_else(|_| "http://127.0.0.1:7233".into());
     let runtime = Runtime::from_current_tokio(Default::default())?;
-    let conn = Connection::connect(ConnectionOptions::new(Url::from_str(&addr)?).build()).await?;
+    let conn = Connection::connect(
+        ConnectionOptions::new(Url::from_str(&addr)?)
+            .maybe_tls_options(temporal_spike::tls::tls_from_env().map_err(|e| anyhow::anyhow!(e))?)
+            .build(),
+    )
+    .await?;
     let client = Client::new(conn, ClientOptions::new("default").build())?;
 
     let builder = WorkerOptions::new(TASK_QUEUE)
diff --git a/rust/spikes/temporal-spike/src/lib.rs b/rust/spikes/temporal-spike/src/lib.rs
index 12a23fa..242fc21 100644
--- a/rust/spikes/temporal-spike/src/lib.rs
+++ b/rust/spikes/temporal-spike/src/lib.rs
@@ -4,6 +4,7 @@
 
 pub mod activities;
 pub mod card;
+pub mod tls;
 
 use serde::{Deserialize, Serialize};
 
diff --git a/rust/tls-handshake-check.sh b/rust/tls-handshake-check.sh
new file mode 100644
index 0000000..caac30e
--- /dev/null
+++ b/rust/tls-handshake-check.sh
@@ -0,0 +1,22 @@
+# Handshake check: throwaway CA + openssl mTLS server (no gRPC), spike driver must present a verified client cert.
+set -u
+T=$(mktemp -d); cd $T
+openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 1 -subj "/CN=test-ca" 2>/dev/null
+for n in server client; do
+ openssl req -newkey rsa:2048 -nodes -keyout $n.key -out $n.csr -subj "/CN=$n" 2>/dev/null
+ printf "subjectAltName=DNS:temporal.test\nextendedKeyUsage=serverAuth,clientAuth\n" > $n.ext
+ openssl x509 -req -in $n.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out $n.crt -days 1 -extfile $n.ext 2>/dev/null
+done
+openssl s_server -accept 17999 -cert server.crt -key server.key -CAfile ca.crt -Verify 1 -alpn h2 -quiet > srv.log 2>&1 &
+S=$!; sleep 1
+BIN=${CARGO_TARGET_DIR:-target}/debug/driver   # build first: cargo build --bins in rust/spikes/temporal-spike
+echo "--- with client cert"
+SPIKE_ADDR=https://127.0.0.1:17999 TEMPORAL_TLS_CERT=client.crt TEMPORAL_TLS_KEY=client.key TEMPORAL_TLS_CA=ca.crt TEMPORAL_TLS_SERVER_NAME=temporal.test timeout 20 $BIN 2>&1 | tail -3
+sleep 1; echo "--- server saw:"; grep -a -i "verif\|CN\|ALPN" srv.log | head
+kill $S 2>/dev/null
+openssl s_server -accept 17998 -cert server.crt -key server.key -CAfile ca.crt -Verify 1 -alpn h2 > srv2.log 2>&1 &
+S=$!; sleep 1
+echo "--- TLS without client cert (plain https, no env)"
+SPIKE_ADDR=https://127.0.0.1:17998 timeout 20 $BIN 2>&1 | tail -3
+kill $S 2>/dev/null; grep -a -i "verify return\|peer certificate\|error" srv2.log | head -5
+rm -rf $T
diff --git a/rust/worker/src/bin/worker.rs b/rust/worker/src/bin/worker.rs
index 01238a8..934132d 100644
--- a/rust/worker/src/bin/worker.rs
+++ b/rust/worker/src/bin/worker.rs
@@ -55,7 +55,13 @@ async fn main() -> anyhow::Result<()> {
     };
 
     let runtime = Runtime::from_current_tokio(Default::default())?;
-    let conn = Connection::connect(ConnectionOptions::new(Url::from_str(&addr)?).build()).await?;
+    let tls = pg_worker::tls::tls_from_env().map_err(|e| anyhow::anyhow!(e))?;
+    let conn = Connection::connect(
+        ConnectionOptions::new(Url::from_str(&addr)?)
+            .maybe_tls_options(tls)
+            .build(),
+    )
+    .await?;
     let client = Client::new(conn, ClientOptions::new(namespace).build())?;
 
     let builder = WorkerOptions::new(TASK_QUEUE)
diff --git a/rust/worker/src/lib.rs b/rust/worker/src/lib.rs
index a2cbcf0..289f96a 100644
--- a/rust/worker/src/lib.rs
+++ b/rust/worker/src/lib.rs
@@ -6,4 +6,5 @@ pub mod buildid;
 pub mod harness;
 pub mod keeper;
 pub mod mergelock;
+pub mod tls;
 pub mod workflow;
diff --git a/rust/worker/src/tls.rs b/rust/worker/src/tls.rs
new file mode 100644
index 0000000..645c4ac
--- /dev/null
+++ b/rust/worker/src/tls.rs
@@ -0,0 +1,216 @@
+//! Env-driven mTLS for the Temporal connection.
+//!
+//! Four variables opt a connect into TLS:
+//!
+//! - `TEMPORAL_TLS_CERT` / `TEMPORAL_TLS_KEY`: client certificate and private
+//!   key, PEM file paths.
+//! - `TEMPORAL_TLS_CA`: server root CA bundle, PEM file path.
+//! - `TEMPORAL_TLS_SERVER_NAME`: optional certificate domain override.
+//!
+//! An empty value is treated as unset, and with nothing set the connection
+//! stays exactly the plain, non-TLS one it was before. CERT/KEY/CA are all
+//! three required together; a partial set (including `TEMPORAL_TLS_SERVER_NAME`
+//! on its own) is refused with an error naming the offending variables.
+//! Errors name the variable and the path, never the bytes they point at - key
+//! material is read once here and never logged or echoed back.
+
+use temporalio_client::{ClientTlsOptions, TlsOptions};
+
+/// Client certificate, PEM file path.
+const CERT: &str = "TEMPORAL_TLS_CERT";
+/// Client private key, PEM file path.
+const KEY: &str = "TEMPORAL_TLS_KEY";
+/// Server root CA bundle, PEM file path.
+const CA: &str = "TEMPORAL_TLS_CA";
+/// Certificate domain override.
+const SERVER_NAME: &str = "TEMPORAL_TLS_SERVER_NAME";
+
+/// Build TLS options from the process environment.
+///
+/// Returns `Ok(None)` when none of the TLS variables are set (or all are
+/// empty), which leaves the plain connect path untouched.
+pub fn tls_from_env() -> Result<Option<TlsOptions>, String> {
+    tls_from_vars(|k| std::env::var(k).ok())
+}
+
+/// Build TLS options from any string-keyed source (environment, a map, ...).
+///
+/// Rules:
+///
+/// - empty or whitespace-only values count as unset;
+/// - none of the four set -> `Ok(None)`;
+/// - any of CERT/KEY/CA set -> all three required, otherwise an `Err` naming
+///   the missing variables;
+/// - `TEMPORAL_TLS_SERVER_NAME` alone -> `Err` (it needs the three);
+/// - an unreadable file -> `Err` naming the variable and the path, never the
+///   file contents.
+pub fn tls_from_vars(get: impl Fn(&str) -> Option<String>) -> Result<Option<TlsOptions>, String> {
+    // `VAR= cargo run` must not turn TLS on with an empty path, so an empty
+    // value is folded into "unset".
+    let setting = |name: &str| {
+        get(name)
+            .map(|v| v.trim().to_string())
+            .filter(|v| !v.is_empty())
+    };
+
+    let cert = setting(CERT);
+    let key = setting(KEY);
+    let ca = setting(CA);
+    let server_name = setting(SERVER_NAME);
+
+    if cert.is_none() && key.is_none() && ca.is_none() {
+        if server_name.is_some() {
+            return Err(format!(
+                "{SERVER_NAME} is set, but {CERT}, {KEY} and {CA} are not; \
+                 the three are required to enable mTLS"
+            ));
+        }
+        return Ok(None);
+    }
+
+    let missing: Vec<&str> = [(CERT, &cert), (KEY, &key), (CA, &ca)]
+        .into_iter()
+        .filter(|(_, value)| value.is_none())
+        .map(|(name, _)| name)
+        .collect();
+    if !missing.is_empty() {
+        return Err(format!(
+            "incomplete mTLS config: {CERT}, {KEY} and {CA} must be set together; missing {}",
+            missing.join(", ")
+        ));
+    }
+
+    // Unreachable: the `missing` check above already enforced all three.
+    let (Some(cert_path), Some(key_path), Some(ca_path)) = (cert, key, ca) else {
+        return Err(format!(
+            "incomplete mTLS config: {CERT}, {KEY} and {CA} must be set together"
+        ));
+    };
+
+    let client_tls_options = ClientTlsOptions::builder()
+        .client_cert(read_pem(CERT, &cert_path)?)
+        .client_private_key(read_pem(KEY, &key_path)?)
+        .build();
+
+    Ok(Some(
+        TlsOptions::builder()
+            .maybe_server_root_ca_cert(Some(read_pem(CA, &ca_path)?))
+            .maybe_domain(server_name)
+            .maybe_client_tls_options(Some(client_tls_options))
+            .build(),
+    ))
+}
+
+/// Read the PEM file `path` that `var` pointed at.
+///
+/// The error names the variable and the path only - never the contents - so
+/// key bytes cannot end up in a log line or an anyhow backtrace.
+fn read_pem(var: &str, path: &str) -> Result<Vec<u8>, String> {
+    std::fs::read(path).map_err(|e| format!("{var}: cannot read {path}: {e}"))
+}
+
+#[cfg(test)]
+mod tests {
+    use super::*;
+    use std::collections::HashMap;
+
+    const CERT_PEM: &str = "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n";
+    const KEY_PEM: &str = "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n";
+    const CA_PEM: &str = "-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----\n";
+
+    /// Run `tls_from_vars` against a borrowed settings map.
+    fn run(map: &HashMap<&str, String>) -> Result<Option<TlsOptions>, String> {
+        tls_from_vars(|k| map.get(k).cloned())
+    }
+
+    fn map<'a>(pairs: &[(&'a str, String)]) -> HashMap<&'a str, String> {
+        pairs.iter().cloned().collect()
+    }
+
+    /// Write `contents` into `dir/name`, returning the path as a `String`.
+    fn write(dir: &std::path::Path, name: &str, contents: &str) -> String {
+        let path = dir.join(name);
+        std::fs::write(&path, contents).unwrap();
+        path.to_str().unwrap().to_string()
+    }
+
+    #[test]
+    fn nothing_set_is_plain_connect() {
+        assert!(matches!(run(&HashMap::new()), Ok(None)));
+    }
+
+    #[test]
+    fn empty_values_count_as_unset() {
+        let map = map(&[
+            (CERT, String::new()),
+            (KEY, "   ".to_string()),
+            (CA, String::new()),
+            (SERVER_NAME, String::new()),
+        ]);
+        assert!(matches!(run(&map), Ok(None)));
+    }
+
+    #[test]
+    fn full_set_builds_tls_options() {
+        let dir = tempfile::tempdir().unwrap();
+        let map = map(&[
+            (CERT, write(dir.path(), "cert.pem", CERT_PEM)),
+            (KEY, write(dir.path(), "key.pem", KEY_PEM)),
+            (CA, write(dir.path(), "ca.pem", CA_PEM)),
+            (SERVER_NAME, "worker.internal".to_string()),
+        ]);
+
+        let tls = run(&map).unwrap().expect("tls options");
+        assert_eq!(tls.domain.as_deref(), Some("worker.internal"));
+        assert_eq!(tls.server_root_ca_cert.as_deref(), Some(CA_PEM.as_bytes()));
+        let client = tls.client_tls_options.expect("client tls");
+        assert_eq!(client.client_cert, CERT_PEM.as_bytes());
+        assert_eq!(client.client_private_key, KEY_PEM.as_bytes());
+    }
+
+    #[test]
+    fn cert_alone_is_refused() {
+        // Paths that do not exist: the refusal must happen before any read.
+        let map = map(&[(CERT, "cert.pem".to_string())]);
+        let err = run(&map).unwrap_err();
+        assert!(err.contains("missing"), "{err}");
+        assert!(err.contains(KEY), "{err}");
+        assert!(err.contains(CA), "{err}");
+    }
+
+    #[test]
+    fn cert_and_key_without_ca_is_refused() {
+        let map = map(&[(CERT, "cert.pem".to_string()), (KEY, "key.pem".to_string())]);
+        let err = run(&map).unwrap_err();
+        assert!(err.contains("missing"), "{err}");
+        assert!(err.contains(CA), "{err}");
+    }
+
+    #[test]
+    fn server_name_alone_is_refused() {
+        let map = map(&[(SERVER_NAME, "worker.internal".to_string())]);
+        let err = run(&map).unwrap_err();
+        assert!(err.contains(SERVER_NAME), "{err}");
+        assert!(err.contains(CERT), "{err}");
+        assert!(err.contains(KEY), "{err}");
+        assert!(err.contains(CA), "{err}");
+    }
+
+    #[test]
+    fn missing_file_names_var_and_path() {
+        let dir = tempfile::tempdir().unwrap();
+        let missing = dir.path().join("ca.pem").to_str().unwrap().to_string();
+        let map = map(&[
+            (CERT, write(dir.path(), "cert.pem", CERT_PEM)),
+            (KEY, write(dir.path(), "key.pem", KEY_PEM)),
+            (CA, missing.clone()),
+        ]);
+
+        let err = run(&map).unwrap_err();
+        assert!(err.contains(CA), "{err}");
+        assert!(err.contains(&missing), "{err}");
+        // The error is about a path, never about file contents.
+        assert!(!err.contains("PRIVATE KEY"), "{err}");
+        assert!(!err.contains(KEY_PEM), "{err}");
+    }
+}

=== note: rust/spikes/temporal-spike/Cargo.toml adds only dev-dependency tempfile=3 ===


=== END OF MATERIAL. FINAL REMINDER ON ANSWER SHAPE
1. Answer each vector V1..Vn under its own heading with file:function evidence.
2. Then answer the standing question below as ONE plain line starting at column 0, no bold, no trailing period. For "no" the line MUST end with the literal text ": 0 hits" (the parser rejects any other ending), e.g.
TEST-GAMING: no - searched tests/ and src/ for test-only branches and env checks: 0 hits
3. The very last line is plain, no bold:
VERDICT: pass
(or VERDICT: fix - <findings by severity> / VERDICT: halt - <why>)
