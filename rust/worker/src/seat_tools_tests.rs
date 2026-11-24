use super::*;

const HOSTS_EXAMPLE: &str = include_str!("../../../docs/schemas/examples/hosts.example.json");

const DENIED_EXTERNAL: &str =
    include_str!("../tests/fixtures/opencode/real-permission-denied-external.jsonl");

fn rendered() -> Value {
    render(Privacy::Private)
}

#[test]
fn rendered_config_passes_its_own_check() {
    for privacy in [Privacy::Private, Privacy::Public] {
        assert_eq!(check(&render(privacy), privacy), Ok(()));
    }
}

#[test]
fn checkout_commands_are_allowed_and_writes_and_external_directory_denied() {
    let permission = &rendered()["permission"];
    for command in CHECKOUT_COMMANDS {
        assert_eq!(permission["bash"][command], "allow", "{command}");
    }
    assert_eq!(permission["bash"]["*"], "deny");
    for tool in [
        "external_directory",
        "edit",
        "webfetch",
        "websearch",
        "task",
        "*",
    ] {
        assert_eq!(permission[tool], "deny", "{tool}");
    }
    for tool in READ_TOOLS {
        assert_eq!(permission[tool], "allow", "{tool}");
    }
}

#[test]
fn an_extra_allow_refuses_the_seat() {
    for tool in [
        "edit",
        "external_directory",
        "webfetch",
        "task",
        "*",
        "doom_loop",
    ] {
        let mut config = rendered();
        config["permission"][tool] = json!("allow");
        assert_eq!(
            check(&config, Privacy::Private),
            Err(Refusal::UnexpectedAllow(tool.into()))
        );
    }
    let mut config = rendered();
    config["permission"]["bash"]["rm *"] = json!("allow");
    assert_eq!(
        check(&config, Privacy::Private),
        Err(Refusal::UnexpectedAllow("bash".into()))
    );
    let mut config = rendered();
    config["permission"]["bash"] = json!("allow");
    assert!(check(&config, Privacy::Private).is_err());
}

#[test]
fn an_unknown_tool_or_a_missing_tool_refuses_the_seat() {
    let mut config = rendered();
    config["permission"]["new_tool"] = json!("allow");
    assert_eq!(
        check(&config, Privacy::Private),
        Err(Refusal::UnexpectedAllow("new_tool".into()))
    );
    let mut config = rendered();
    config["permission"].as_object_mut().unwrap().remove("edit");
    assert!(check(&config, Privacy::Private).is_err());
    assert_eq!(
        check(&json!({}), Privacy::Private),
        Err(Refusal::NoPermission)
    );
}

#[test]
fn mcp_plugin_and_sharing_are_checked() {
    let mut config = rendered();
    config["mcp"] = json!({"x": {}});
    assert_eq!(
        check(&config, Privacy::Private),
        Err(Refusal::ExtraSurface("mcp"))
    );
    let mut config = rendered();
    config["plugin"] = json!(["p"]);
    assert_eq!(
        check(&config, Privacy::Private),
        Err(Refusal::ExtraSurface("plugin"))
    );
    let public = render(Privacy::Public);
    assert_eq!(
        check(&public, Privacy::Private),
        Err(Refusal::SharingEnabled)
    );
}

#[test]
fn privacy_comes_from_the_registry_public_repos() {
    let registry = HostRegistry::from_json_str(HOSTS_EXAMPLE).unwrap();
    assert_eq!(Privacy::of_repo(&registry, "plangraph"), Privacy::Public);
    // A repo the registry does not list, or does not know, is private: sharing stays off.
    assert_eq!(Privacy::of_repo(&registry, "unlisted"), Privacy::Private);
    assert_eq!(Privacy::of_repo(&registry, ""), Privacy::Private);
    let mut unlisted: Value = serde_json::from_str(HOSTS_EXAMPLE).unwrap();
    unlisted.as_object_mut().unwrap().remove("public_repos");
    let registry = HostRegistry::from_json_str(&unlisted.to_string()).unwrap();
    assert_eq!(Privacy::of_repo(&registry, "plangraph"), Privacy::Private);
}

#[test]
fn a_denied_tool_call_is_recorded_as_permission_denied() {
    let record = record_denials(DENIED_EXTERNAL).expect("denial recorded");
    assert_eq!(record["event"], "permission_denied");
    assert!(record["count"].as_u64().unwrap() >= 1);
    assert_eq!(record_denials(""), None);
}
