//! CODEX-HOST-READINESS: explicit writer identity setup and effective identity checks.
use crate::{
    errors::ToolError,
    package::{Backend, Host},
    placement::parameter,
};
use serde_json::{json, Value};
fn allowed(email: &str) -> bool {
    regex::Regex::new(r"^(?:[0-9]+\+)?[A-Za-z0-9._\[\]-]+@users\.noreply\.github\.com$")
        .unwrap()
        .is_match(email)
}
fn ready(v: &Value) -> bool {
    !v["name"].as_str().unwrap_or("").trim().is_empty()
        && ["email", "author_email", "committer_email"]
            .iter()
            .all(|k| v[*k].as_str().is_some_and(allowed))
}
fn setup_identity(
    name: Option<String>,
    email: Option<String>,
) -> Result<(String, String), ToolError> {
    let name = name
        .filter(|n| !n.trim().is_empty() && !n.chars().any(char::is_control))
        .ok_or_else(|| parameter("setup requires PG_WRITER_NAME"))?;
    let email = email
        .filter(|e| allowed(e))
        .ok_or_else(|| parameter("setup requires a noreply PG_WRITER_EMAIL"))?;
    Ok((name, email))
}
fn script(host: &Host, setup: Option<(&str, &str)>) -> String {
    use crate::package_scripts::{ps, sh};
    if host.kind == "windows" {
        format!(
            r#"$ErrorActionPreference='Stop'
Set-Location {repo}
{setup}
$name=(& git config user.name)
$email=(& git config user.email)
$author=(& git var GIT_AUTHOR_IDENT) -replace '^.*<([^>]+)>.*$', '$1'
$committer=(& git var GIT_COMMITTER_IDENT) -replace '^.*<([^>]+)>.*$', '$1'
@{{name=$name;email=$email;author_email=$author;committer_email=$committer}} | ConvertTo-Json -Compress
"#,
            repo = ps(&host.repo),
            setup = if let Some((name, email)) = setup {
                format!(
                    "git config --global user.name {}\ngit config --global user.email {}",
                    ps(name),
                    ps(email)
                )
            } else {
                String::new()
            }
        )
    } else {
        format!(
            r#"set -eu
cd {repo}
{setup}
hex() {{ od -An -tx1 | tr -d ' \n'; }}
name=$(git config user.name | tr -d '\n' | hex)
email=$(git config user.email | tr -d '\n' | hex)
author=$(git var GIT_AUTHOR_IDENT | sed -n 's/.*<\([^>]*\)>.*/\1/p' | tr -d '\n' | hex)
committer=$(git var GIT_COMMITTER_IDENT | sed -n 's/.*<\([^>]*\)>.*/\1/p' | tr -d '\n' | hex)
printf '{{"name_hex":"%s","email_hex":"%s","author_email_hex":"%s","committer_email_hex":"%s"}}\n' "$name" "$email" "$author" "$committer"
"#,
            repo = sh(&host.repo),
            setup = if let Some((name, email)) = setup {
                format!(
                    "git config --global user.name {}\ngit config --global user.email {}",
                    sh(name),
                    sh(email)
                )
            } else {
                String::new()
            }
        )
    }
}
pub(crate) fn check(
    host: &Host,
    setup: bool,
    backend: &mut impl Backend,
) -> Result<Value, ToolError> {
    let identity = if setup {
        Some(setup_identity(
            std::env::var("PG_WRITER_NAME").ok(),
            std::env::var("PG_WRITER_EMAIL").ok(),
        )?)
    } else {
        None
    };
    let mut result = backend.remote(
        host,
        &script(
            host,
            identity
                .as_ref()
                .map(|(name, email)| (name.as_str(), email.as_str())),
        ),
    )?;
    if host.kind == "linux" {
        for key in ["name", "email", "author_email", "committer_email"] {
            result[key] = json!(crate::package_probe::unhex(&result[format!("{key}_hex")])?);
        }
    }
    result["ready"] = json!(ready(&result));
    Ok(result)
}
pub(crate) fn cli(args: Vec<String>, out: &mut dyn std::io::Write) -> Result<(), String> {
    if !(args.len() == 4 || args.len() == 5 && args[4] == "--setup-identity")
        || args[0] != "--state"
        || args[2] != "--host"
    {
        return Err(
            "host-readiness --state <path> --host <configured host> [--setup-identity]".into(),
        );
    }
    let run = || -> Result<Value, ToolError> {
        let host = crate::package::configured_host(std::path::Path::new(&args[1]), &args[3])?;
        check(
            &host,
            args.len() == 5,
            &mut crate::package_transport::Native::default(),
        )
    };
    let value = run().map_err(|e| e.into_error_data().message.to_string())?;
    writeln!(out, "{value}").map_err(|e| e.to_string())?;
    if value["ready"] != true {
        return Err(parameter("writer host Git identity refused")
            .into_error_data()
            .message
            .to_string());
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn host_readiness_requires_name_and_effective_noreply_identities() {
        let email = ["123+fixture", "users.noreply.github.com"].join("@");
        let valid =
            json!({"name":"Fixture", "email":email,"author_email":email,"committer_email":email});
        assert!(ready(&valid));
        for key in ["name", "email", "author_email", "committer_email"] {
            let mut v = valid.clone();
            v[key] = json!(if key == "name" {
                ""
            } else {
                "writer@fixture.invalid"
            });
            assert!(!ready(&v));
        }
    }
    #[test]
    fn host_readiness_setup_requires_operator_identity() {
        let email = ["123+fixture", "users.noreply.github.com"].join("@");
        assert!(setup_identity(None, Some(email.clone())).is_err());
        assert!(setup_identity(Some("Fixture".into()), None).is_err());
        assert!(setup_identity(Some("".into()), Some(email.clone())).is_err());
        assert!(setup_identity(
            Some("Fixture".into()),
            Some("fixture@example.invalid".into())
        )
        .is_err());
        let (name, email) = setup_identity(Some("Fixture Writer".into()), Some(email))
            .unwrap_or_else(|_| panic!("fixture identity"));
        for kind in ["linux", "windows"] {
            let host = Host {
                kind: kind.into(),
                target: "fixture".into(),
                repo: "/tmp/fixture".into(),
                root: "/tmp".into(),
            };
            let setup = script(&host, Some((&name, &email)));
            assert!(setup.contains("Fixture Writer"));
            assert!(setup.contains(&email));
            assert!(!script(&host, None).contains("config --global"));
        }
    }
}
