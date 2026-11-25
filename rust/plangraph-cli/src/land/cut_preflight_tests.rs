//! The preflight asks the pinned gate itself: its `GATE_PATHS`, its Python `fnmatch`, run isolated.
use super::cut_check_tests::{fx_with_base_gate, python, run_check, stub_gate_paths};
use super::cut_gate::PinnedGate;

/// The preflight asks the pinned gate's own `GATE_PATHS` through Python's `fnmatch`: a character class, `?`
/// on a multi-byte name and bytes that are not UTF-8 give the answers the gate gives.
#[test]
fn the_preflight_matches_paths_like_the_pinned_gate() {
    let dir = tempfile::tempdir().unwrap();
    let checkout = tempfile::tempdir().unwrap();
    let gate = dir.path().join("gate.py");
    std::fs::write(
        &gate,
        stub_gate_paths("('[ab]*.txt', 'dir/?.md', 'tools/gate.py*')", "pass"),
    )
    .unwrap();
    let pinned = PinnedGate {
        gate: &gate,
        checkout: checkout.path(),
        python: &python,
    };
    let table: [(&[u8], bool); 9] = [
        (b"a1.txt", true),
        (b"c1.txt", false),
        (b"dir/x.md", true),
        ("dir/\u{fc}.md".as_bytes(), true),
        (b"dir/ab.md", false),
        (b"dir/\xff.md", true),
        (b"tools/gate.py.bak", true),
        (b"z/tools/gate.py", false),
        (b"b.txt", true),
    ];
    for (path, want) in table {
        let got = !pinned.matching(&[path], None).unwrap().hits.is_empty();
        assert_eq!(got, want, "{:?}", String::from_utf8_lossy(path));
    }
    // A gate that cannot answer is a refusal, not a pass.
    std::fs::write(&gate, "raise SystemExit(3)\n").unwrap();
    assert!(pinned.matching(&[b"a.txt"], None).is_err());
}

#[test]
fn a_character_class_in_the_pinned_gate_does_not_reject_every_range() {
    let fx = fx_with_base_gate(&stub_gate_paths("('[ab]*.txt', 'dir/?.md')", "sys.exit(0)"));
    let plain = fx.hand_cut("c.txt", "");
    run_check(&fx, &plain).unwrap();
    for file in ["a1.txt", "dir/\u{fc}.md"] {
        let head = fx.hand_cut(file, "");
        let err = run_check(&fx, &head).unwrap_err();
        assert!(
            err.contains(&head) && err.contains("grants are not accepted yet"),
            "{err}"
        );
    }
}

/// The one Rust copy of a Python rule left, `py_lines`, is held to Python's own `str.splitlines()`.
#[test]
fn py_lines_agrees_with_python_splitlines_on_every_separator() {
    use super::cut_gate::py_lines;
    use std::io::Write;
    use std::process::Stdio;
    let seps = [
        "\n", "\r", "\r\n", "\u{b}", "\u{c}", "\u{1c}", "\u{1d}", "\u{1e}", "\u{85}", "\u{2028}",
        "\u{2029}", "\n\r", "\r\r\n", "",
    ];
    let mut texts: Vec<String> = vec![String::new(), "a".into(), "\n".into(), "a\n".into()];
    for sep in seps {
        texts.push(format!("a{sep}b"));
        texts.push(format!("{sep}a{sep}"));
        texts.push(format!("a{sep}{sep}b{sep}"));
    }
    for text in texts {
        let mut child = python()
            .args([
                "-I",
                "-c",
                "import json, sys; print(json.dumps(sys.stdin.buffer.read().decode('utf-8').splitlines()))",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(text.as_bytes())
            .unwrap();
        let out = child.wait_with_output().unwrap();
        // Bytes in, so no newline translation hides a CR.
        let want: Vec<String> = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(py_lines(&text), want, "{text:?}");
    }
}

#[test]
fn py_lines_breaks_where_python_splitlines_does() {
    use super::cut_gate::py_lines;
    assert_eq!(
        py_lines("a\r\nb\rc\u{2028}d\u{c}e\n"),
        ["a", "b", "c", "d", "e"]
    );
    assert_eq!(py_lines("a\n\nb"), ["a", "", "b"]);
    assert!(py_lines("").is_empty());
}
