//! Exercise the native fetch pipeline through pipes, never network sockets.
use super::*;
use gix::protocol::transport::{self, client::blocking_io::Transport, Protocol};
use std::{borrow::Cow, io, process::Stdio};

#[test]
fn mirror_native_fetch_receives_main_and_forced_candidates_without_network() {
    let f = Fixture::new();
    let a = f.commit(None);
    git(&f.origin, &["update-ref", "refs/heads/other", &a]);
    git(&f.origin, &["update-ref", "refs/tags/fixture", &a]);
    let candidate = f._tmp.path().join("candidate.git");
    backend::init(&candidate).unwrap();
    let repo = open(&candidate).unwrap();
    for scheme in ["http", "https", "git"] {
        let b = f.commit(Some(&a));
        // The rewind must still be received into the private candidate ref;
        // Mirror's ancestry check, not gix's ref update, decides acceptance.
        for head in [&b, &a] {
            git(&f.origin, &["update-ref", "refs/heads/main", head]);
            let url = format!("{scheme}://example.invalid/repository.git");
            let mut child = None;
            let received = backend::fetch_with_connector(&repo, &url, |actual, version| {
                assert_eq!(actual.to_bstring().to_string(), url);
                assert_eq!(version, Protocol::V2);
                // No file transport/helper in production: this process is the
                // disposable bare remote's test-only server, connected by pipes.
                let mut process = git_command(&f.origin)
                    .args(["upload-pack", "--strict", "."])
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .spawn()
                    .unwrap();
                let transport = transport::client::git::blocking_io::Connection::new(
                    process.stdout.take().unwrap(),
                    process.stdin.take().unwrap(),
                    version,
                    actual.path,
                    None::<(String, Option<u16>)>,
                    transport::client::git::ConnectMode::Process,
                    false,
                );
                child = Some(process);
                Ok(Box::new(transport))
            });
            assert!(child.unwrap().wait_with_output().unwrap().status.success());
            let received = received.unwrap();
            assert_eq!(received.to_string(), *head);
            assert_eq!(
                git(&candidate, &["for-each-ref", "--format=%(refname)"]),
                "refs/pg-ctx/main"
            );
            assert_eq!(git(&candidate, &["rev-parse", "refs/pg-ctx/main"]), *head);
            assert!(repo.find_commit(received).is_ok());
        }
    }
}

#[test]
fn mirror_native_fetch_without_main_never_returns_stale_ref_or_refreshes_watermark() {
    let f = Fixture::new();
    let head = f.commit(None);
    let watermark = f.mirror.fetch_at("demo", 100).unwrap();
    let bytes = f.bytes();
    let candidate = f._tmp.path().join("candidate.git");
    copy_repository(&f.mirror.mirror_dir("demo").unwrap(), &candidate).unwrap();
    let repo = open(&candidate).unwrap();
    for scheme in ["http", "https", "git"] {
        for protocol in ["", "version=2"] {
            let url = format!("{scheme}://example.invalid/repository.git");
            let fetch = || {
                let mut child = None;
                let result = backend::fetch_with_connector(&repo, &url, |actual, version| {
                    assert_eq!(actual.to_bstring().to_string(), url);
                    assert_eq!(version, Protocol::V2);
                    let mut process = git_command(&f.origin)
                        .env("GIT_PROTOCOL", protocol)
                        .args(["upload-pack", "--strict", "."])
                        .stdin(Stdio::piped())
                        .stdout(Stdio::piped())
                        .stderr(Stdio::piped())
                        .spawn()
                        .unwrap();
                    let transport = transport::client::git::blocking_io::Connection::new(
                        process.stdout.take().unwrap(),
                        process.stdin.take().unwrap(),
                        version,
                        actual.path,
                        None::<(String, Option<u16>)>,
                        transport::client::git::ConnectMode::Process,
                        false,
                    );
                    child = Some(process);
                    Ok(Box::new(transport))
                });
                // Missing-ref validation may disconnect before upload-pack's
                // final flush; always reap the server, including on refusal.
                let output = child.unwrap().wait_with_output().unwrap();
                if result.is_ok() {
                    assert!(output.status.success());
                }
                result
            };
            git(&f.origin, &["update-ref", "refs/heads/main", &head]);
            assert_eq!(
                fetch().map(|id| id.to_string()),
                Ok(head.clone()),
                "{scheme} {protocol}"
            );
            git(&candidate, &["pack-refs", "--all"]);
            git(&f.origin, &["update-ref", "-d", "refs/heads/main"]);
            git(&f.origin, &["update-ref", "refs/heads/other", &head]);
            // Both a remote advertising only unrelated refs and an empty remote
            // must fail despite the previous successful fetch's packed ref.
            // Pinned gix validates the exact refspec in receive(); the backend's
            // error propagation stops Mirror before publication or freshness writes.
            for empty in [false, true] {
                if empty {
                    git(&f.origin, &["update-ref", "-d", "refs/heads/other"]);
                }
                assert_eq!(fetch(), Err(Alarm::FetchFailed), "{scheme} {protocol}");
                assert_eq!(git(&candidate, &["rev-parse", "refs/pg-ctx/main"]), head);
                assert_eq!(f.bytes(), bytes);
                f.assert_persisted(&watermark);
            }
        }
    }
}

struct Denied;
impl transport::client::TransportWithoutIO for Denied {
    fn to_url(&self) -> Cow<'_, gix::bstr::BStr> {
        Cow::Borrowed(gix::bstr::BStr::new(
            b"https://example.invalid/repository.git",
        ))
    }
    fn connection_persists_across_multiple_requests(&self) -> bool {
        true
    }
    fn configure(&mut self, _: &dyn std::any::Any) -> gix::Result<()> {
        Ok(())
    }
}
impl Transport for Denied {
    fn handshake<'a>(
        &mut self,
        _: transport::Service,
        _: &'a [(&'a str, Option<&'a str>)],
    ) -> Result<transport::client::blocking_io::SetServiceResponse<'_>, transport::client::Error>
    {
        Err(io::Error::new(io::ErrorKind::PermissionDenied, "authentication required").into())
    }
    fn request(
        &mut self,
        _: transport::client::WriteMode,
        _: transport::client::MessageKind,
        _: bool,
    ) -> Result<transport::client::blocking_io::RequestWriter<'_>, transport::client::Error> {
        // gix may try a best-effort flush when dropping a failed connection.
        Err(io::Error::new(io::ErrorKind::PermissionDenied, "connection refused").into())
    }
}

#[cfg(unix)]
#[test]
fn mirror_native_fetch_never_uses_credential_helpers_after_authentication_refusal() {
    let f = Fixture::new();
    let candidate = f._tmp.path().join("candidate.git");
    backend::init(&candidate).unwrap();
    let marker = f._tmp.path().join("helper-ran");
    let config = candidate.join("config");
    let mut contents = fs::read_to_string(&config).unwrap();
    contents.push_str(&format!(
        "\n[credential]\n\thelper = \"!echo invoked > '{}'; exit 1\"\n",
        marker.display()
    ));
    fs::write(config, contents).unwrap();
    let repo = open(&candidate).unwrap();
    let result =
        backend::fetch_with_connector(&repo, "https://example.invalid/repository.git", |_, _| {
            Ok(Box::new(Denied))
        });
    assert_eq!(result, Err(Alarm::FetchFailed));
    assert!(!marker.exists(), "credential helper was invoked");
    assert!(git(&candidate, &["for-each-ref"]).is_empty());
}
