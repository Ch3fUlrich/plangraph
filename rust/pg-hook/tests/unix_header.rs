#![cfg(unix)]
//! Every top-level pg-hook test file is Unix-only unless `portable.txt` lists it, because the
//! Windows job builds only the portable ones (tests/test_ci_select_rust_inputs.py).
//! A new test file starts with the first line `#![cfg(unix)]`, as this file does.

use std::{fs, path::Path};

fn names(dir: &Path) -> Vec<String> {
    let mut files: Vec<String> = fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .filter(|name| name.ends_with(".rs"))
        .collect();
    files.sort();
    files
}

#[test]
fn test_files_are_unix_only_unless_listed_portable() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests");
    let portable = fs::read_to_string(dir.join("portable.txt")).unwrap();
    let portable: Vec<&str> = portable.lines().filter(|line| !line.is_empty()).collect();
    let mut problems = Vec::new();
    for name in names(&dir) {
        let unix_only = fs::read_to_string(dir.join(&name))
            .unwrap()
            .starts_with("#![cfg(unix)]\n");
        match (unix_only, portable.contains(&name.as_str())) {
            (false, false) => problems.push(format!(
                "rust/pg-hook/tests/{name}: add `#![cfg(unix)]` as the first line (the Windows job \
                 does not build pg-hook tests); list it in tests/portable.txt only if it is portable"
            )),
            (true, true) => problems.push(format!(
                "rust/pg-hook/tests/{name}: listed in portable.txt but starts with `#![cfg(unix)]`"
            )),
            _ => {}
        }
    }
    for name in portable {
        if !dir.join(name).is_file() {
            problems.push(format!(
                "tests/portable.txt names {name}, which does not exist"
            ));
        }
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}
