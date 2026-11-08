use super::*;
#[test]
fn private_words_and_unavailable_lists() {
    let dir = std::env::temp_dir().join(format!(
        "pg-words-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("words.txt");
    let word = ["synth", "word"].concat();
    let regex_word = ["mock", "pattern"].concat();
    std::fs::write(
        &path,
        format!("# comment\n\n{word}\nre:{regex_word}[0-9]+\n"),
    )
    .unwrap();
    let mut view = json!({"hosts":[{"host":word.to_uppercase(),"readable":true,"agents":[{
        "name":format!("_{word}-{}",word.to_uppercase()), "item":format!("{regex_word}42"),
        "model":format!("long{word}tail"), "state":word,"host":word}]}]});
    mask_words(&mut view, &path);
    assert_eq!(view["hosts"][0]["host"], "[redacted]");
    let row = &view["hosts"][0]["agents"][0];
    assert_eq!(row["name"], "_[redacted]-[redacted]");
    assert_eq!(row["item"], "[redacted]");
    assert_eq!(row["model"], format!("long{word}tail"));
    assert_eq!(row["state"], "[redacted]");
    assert_eq!(row["host"], "[redacted]");
    for contents in [None, Some("re:[")] {
        if let Some(contents) = contents {
            std::fs::write(&path, contents).unwrap();
        } else {
            std::fs::remove_file(&path).unwrap();
        }
        mask_words(&mut view, &path);
        assert_eq!(view["hosts"][0]["reason"], "scrub_words_unavailable");
        assert_eq!(view["hosts"][0]["readable"], false);
        assert_eq!(view["hosts"][0]["agents"], json!([]));
    }
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
fn scalars_aliases_age_and_runtime() {
    let row = project(
        &json!({"name":true,"run_id":"worker","item_key":"PROJECT:ITEM","runtime":"invalid","started":"1970-01-01T00:00:10Z","model":[],"credential":"excluded"}),
        "local",
        None,
        Some(20.0),
    );
    assert_eq!(row["name"], "worker");
    assert_eq!(row["age_s"], 10.0);
    assert!(row["runtime"].is_null());
    assert!(row["model"].is_null());
    assert_eq!(row.as_object().unwrap().len(), 7);
    assert!(!row.to_string().contains("excluded"));
}
#[test]
fn malformed_hosts_and_rows_isolated() {
    let view = project_view(
        &json!({"hosts":[null,{"host":"local","readable":true,"agents":[null,{"name":"live","host":"spoofed"}]}]}),
    );
    assert_eq!(view["hosts"][0]["readable"], false);
    assert!(view["hosts"][1]["agents"][0]["name"].is_null());
    assert_eq!(view["hosts"][1]["agents"][1]["host"], "local");
}
#[test]
fn future_age_is_unknown() {
    assert!(project(&json!({"started":30}), "local", None, Some(20.0))["age_s"].is_null());
}
