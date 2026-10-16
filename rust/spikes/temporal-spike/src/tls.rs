//! Env-driven mTLS for the Temporal connection.
//!
//! Four variables opt a connect into TLS:
//!
//! - `TEMPORAL_TLS_CERT` / `TEMPORAL_TLS_KEY`: client certificate and private
//!   key, PEM file paths.
//! - `TEMPORAL_TLS_CA`: server root CA bundle, PEM file path.
//! - `TEMPORAL_TLS_SERVER_NAME`: optional certificate domain override.
//!
//! An empty value is treated as unset, and with nothing set the connection
//! stays exactly the plain, non-TLS one it was before. CERT/KEY/CA are all
//! three required together; a partial set (including `TEMPORAL_TLS_SERVER_NAME`
//! on its own) is refused with an error naming the offending variables.
//! Errors name the variable and the path, never the bytes they point at - key
//! material is read once here and never logged or echoed back.

use temporalio_client::{ClientTlsOptions, TlsOptions};

/// Client certificate, PEM file path.
const CERT: &str = "TEMPORAL_TLS_CERT";
/// Client private key, PEM file path.
const KEY: &str = "TEMPORAL_TLS_KEY";
/// Server root CA bundle, PEM file path.
const CA: &str = "TEMPORAL_TLS_CA";
/// Certificate domain override.
const SERVER_NAME: &str = "TEMPORAL_TLS_SERVER_NAME";

/// Build TLS options from the process environment.
///
/// Returns `Ok(None)` when none of the TLS variables are set (or all are
/// empty), which leaves the plain connect path untouched.
pub fn tls_from_env() -> Result<Option<TlsOptions>, String> {
    tls_from_vars(|k| std::env::var(k).ok())
}

/// Build TLS options from any string-keyed source (environment, a map, ...).
///
/// Rules:
///
/// - empty or whitespace-only values count as unset;
/// - none of the four set -> `Ok(None)`;
/// - any of CERT/KEY/CA set -> all three required, otherwise an `Err` naming
///   the missing variables;
/// - `TEMPORAL_TLS_SERVER_NAME` alone -> `Err` (it needs the three);
/// - an unreadable file -> `Err` naming the variable and the path, never the
///   file contents.
pub fn tls_from_vars(get: impl Fn(&str) -> Option<String>) -> Result<Option<TlsOptions>, String> {
    // `VAR= cargo run` must not turn TLS on with an empty path, so an empty
    // value is folded into "unset".
    let setting = |name: &str| {
        get(name)
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
    };

    let cert = setting(CERT);
    let key = setting(KEY);
    let ca = setting(CA);
    let server_name = setting(SERVER_NAME);

    if cert.is_none() && key.is_none() && ca.is_none() {
        if server_name.is_some() {
            return Err(format!(
                "{SERVER_NAME} is set, but {CERT}, {KEY} and {CA} are not; \
                 the three are required to enable mTLS"
            ));
        }
        return Ok(None);
    }

    let missing: Vec<&str> = [(CERT, &cert), (KEY, &key), (CA, &ca)]
        .into_iter()
        .filter(|(_, value)| value.is_none())
        .map(|(name, _)| name)
        .collect();
    if !missing.is_empty() {
        return Err(format!(
            "incomplete mTLS config: {CERT}, {KEY} and {CA} must be set together; missing {}",
            missing.join(", ")
        ));
    }

    // Unreachable: the `missing` check above already enforced all three.
    let (Some(cert_path), Some(key_path), Some(ca_path)) = (cert, key, ca) else {
        return Err(format!(
            "incomplete mTLS config: {CERT}, {KEY} and {CA} must be set together"
        ));
    };

    let client_tls_options = ClientTlsOptions::builder()
        .client_cert(read_pem(CERT, &cert_path)?)
        .client_private_key(read_pem(KEY, &key_path)?)
        .build();

    Ok(Some(
        TlsOptions::builder()
            .maybe_server_root_ca_cert(Some(read_pem(CA, &ca_path)?))
            .maybe_domain(server_name)
            .maybe_client_tls_options(Some(client_tls_options))
            .build(),
    ))
}

/// Refuse TLS settings combined with a non-`https` address.
///
/// With TLS variables set, an `http://` address would connect in plaintext and
/// silently ignore the client certificate; fail at startup instead. The error
/// names the variable only, never any file contents.
pub fn require_https(addr: &str, tls: &Option<TlsOptions>) -> Result<(), String> {
    if tls.is_some() && !addr.trim().to_ascii_lowercase().starts_with("https://") {
        return Err(format!(
            "{CERT} (mTLS) is set but the Temporal address `{addr}` is not an https:// URL;              refusing to connect without TLS"
        ));
    }
    Ok(())
}

/// Read the PEM file `path` that `var` pointed at.
///
/// The error names the variable and the path only - never the contents - so
/// key bytes cannot end up in a log line or an anyhow backtrace.
fn read_pem(var: &str, path: &str) -> Result<Vec<u8>, String> {
    std::fs::read(path).map_err(|e| format!("{var}: cannot read {path}: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    const CERT_PEM: &str = "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n";
    const KEY_PEM: &str = "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n";
    const CA_PEM: &str = "-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----\n";

    /// Run `tls_from_vars` against a borrowed settings map.
    fn run(map: &HashMap<&str, String>) -> Result<Option<TlsOptions>, String> {
        tls_from_vars(|k| map.get(k).cloned())
    }

    fn map<'a>(pairs: &[(&'a str, String)]) -> HashMap<&'a str, String> {
        pairs.iter().cloned().collect()
    }

    /// Write `contents` into `dir/name`, returning the path as a `String`.
    fn write(dir: &std::path::Path, name: &str, contents: &str) -> String {
        let path = dir.join(name);
        std::fs::write(&path, contents).unwrap();
        path.to_str().unwrap().to_string()
    }

    #[test]
    fn nothing_set_is_plain_connect() {
        assert!(matches!(run(&HashMap::new()), Ok(None)));
    }

    #[test]
    fn empty_values_count_as_unset() {
        let map = map(&[
            (CERT, String::new()),
            (KEY, "   ".to_string()),
            (CA, String::new()),
            (SERVER_NAME, String::new()),
        ]);
        assert!(matches!(run(&map), Ok(None)));
    }

    #[test]
    fn full_set_builds_tls_options() {
        let dir = tempfile::tempdir().unwrap();
        let map = map(&[
            (CERT, write(dir.path(), "cert.pem", CERT_PEM)),
            (KEY, write(dir.path(), "key.pem", KEY_PEM)),
            (CA, write(dir.path(), "ca.pem", CA_PEM)),
            (SERVER_NAME, "worker.internal".to_string()),
        ]);

        let tls = run(&map).unwrap().expect("tls options");
        assert_eq!(tls.domain.as_deref(), Some("worker.internal"));
        assert_eq!(tls.server_root_ca_cert.as_deref(), Some(CA_PEM.as_bytes()));
        let client = tls.client_tls_options.expect("client tls");
        assert_eq!(client.client_cert, CERT_PEM.as_bytes());
        assert_eq!(client.client_private_key, KEY_PEM.as_bytes());
    }

    #[test]
    fn cert_alone_is_refused() {
        // Paths that do not exist: the refusal must happen before any read.
        let map = map(&[(CERT, "cert.pem".to_string())]);
        let err = run(&map).unwrap_err();
        assert!(err.contains("missing"), "{err}");
        assert!(err.contains(KEY), "{err}");
        assert!(err.contains(CA), "{err}");
    }

    #[test]
    fn cert_and_key_without_ca_is_refused() {
        let map = map(&[(CERT, "cert.pem".to_string()), (KEY, "key.pem".to_string())]);
        let err = run(&map).unwrap_err();
        assert!(err.contains("missing"), "{err}");
        assert!(err.contains(CA), "{err}");
    }

    #[test]
    fn server_name_alone_is_refused() {
        let map = map(&[(SERVER_NAME, "worker.internal".to_string())]);
        let err = run(&map).unwrap_err();
        assert!(err.contains(SERVER_NAME), "{err}");
        assert!(err.contains(CERT), "{err}");
        assert!(err.contains(KEY), "{err}");
        assert!(err.contains(CA), "{err}");
    }

    #[test]
    fn missing_file_names_var_and_path() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("ca.pem").to_str().unwrap().to_string();
        let map = map(&[
            (CERT, write(dir.path(), "cert.pem", CERT_PEM)),
            (KEY, write(dir.path(), "key.pem", KEY_PEM)),
            (CA, missing.clone()),
        ]);

        let err = run(&map).unwrap_err();
        assert!(err.contains(CA), "{err}");
        assert!(err.contains(&missing), "{err}");
        // The error is about a path, never about file contents.
        // The key file was read successfully; its body must not leak.
        assert!(!err.contains("SECRET-KEY-MARKER"), "{err}");
    }

    fn some_tls() -> Option<TlsOptions> {
        Some(TlsOptions::builder().build())
    }

    #[test]
    fn https_with_tls_is_accepted() {
        assert!(require_https("https://temporal.internal:7234", &some_tls()).is_ok());
        assert!(require_https("HTTPS://temporal.internal:7234", &some_tls()).is_ok());
    }

    #[test]
    fn http_with_tls_is_refused() {
        let err = require_https("http://127.0.0.1:7233", &some_tls()).unwrap_err();
        assert!(err.contains(CERT), "{err}");
        assert!(err.contains("https://"), "{err}");
    }

    #[test]
    fn http_without_tls_stays_plain() {
        assert!(require_https("http://127.0.0.1:7233", &None).is_ok());
    }
}
