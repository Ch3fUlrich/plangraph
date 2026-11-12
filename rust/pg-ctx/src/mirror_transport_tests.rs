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
