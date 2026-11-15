use super::*;
#[test]
fn final_head_review_ignores_old_head_and_requires_last_canonical_verdict() {
    let dir = std::env::temp_dir().join(format!("native-review-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("aaaaaaaa-hydrogen.txt"), "VERDICT: ACCEPT\n").unwrap();
    assert!(find_review(&dir, "bbbbbbbbbbbbbbbb").unwrap().is_none());
    fs::write(
        dir.join("bbbbbbbb-hydrogen.txt"),
        "VERDICT: ACCEPT\nVERDICT: UNKNOWN\n",
    )
    .unwrap();
    assert!(find_review(&dir, "bbbbbbbbbbbbbbbb").unwrap().is_none());
    fs::write(
        dir.join("bbbbbbbb-hydrogen.txt"),
        "VERDICT: REQUEST_CHANGES\n",
    )
    .unwrap();
    assert_eq!(
        find_review(&dir, "bbbbbbbbbbbbbbbb").unwrap().unwrap()["verdict"],
        "REQUEST_CHANGES"
    );
    fs::remove_dir_all(dir).unwrap();
}
