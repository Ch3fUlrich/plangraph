//! The preflight asks the pinned gate itself: its `GATE_PATHS`, its Python `fnmatch`, run isolated.
use super::build_tests::g;
use super::cut_refusal_tests::{fx_with_base_gate, run_cut, stub_gate, stub_gate_paths};
use super::cut_tests::{python, Fx, TRAILERS};

/// The preflight asks the pinned gate's own `GATE_PATHS` through Python's `fnmatch`: a character class, `?`
/// on a multi-byte name and bytes that are not UTF-8 give the answers the gate gives.
#[test]
fn the_preflight_matches_paths_like_the_pinned_gate() {
    use super::cut_gate::PinnedGate;
    let dir = tempfile::tempdir().unwrap();
    let gate = dir.path().join("gate.py");
    std::fs::write(
        &gate,
        stub_gate_paths("('[ab]*.txt', 'dir/?.md', 'tools/gate.py*')", "pass"),
    )
    .unwrap();
    let pinned = PinnedGate {
        gate: &gate,
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
        let got = !pinned.matching(&[path]).unwrap().is_empty();
        assert_eq!(got, want, "{:?}", String::from_utf8_lossy(path));
    }
    // A gate that cannot answer is a refusal, not a pass.
    std::fs::write(&gate, "raise SystemExit(3)\n").unwrap();
    assert!(pinned.matching(&[b"a.txt"]).is_err());
}

#[test]
fn a_character_class_in_the_pinned_gate_does_not_reject_every_range() {
    let gate = stub_gate_paths("('[ab]*.txt', 'dir/?.md')", "sys.exit(0)");
    let fx = fx_with_base_gate(&gate);
    let plain = fx.source("c.txt", TRAILERS);
    run_cut(&fx, "slice/ok", &plain, &[]).unwrap();
    for (name, file) in [("slice/class", "a1.txt"), ("slice/mb", "dir/\u{fc}.md")] {
        let src = fx.source(file, TRAILERS);
        let err = run_cut(&fx, name, &src, &[]).unwrap_err();
        assert!(
            err.contains(&src) && err.contains("grants are not accepted yet"),
            "{err}"
        );
    }
}

/// A module in the checkout must not run before the gate's code: the loader and the gate run isolated.
#[test]
fn a_checkout_local_module_cannot_shadow_the_loader() {
    let fx = fx_with_base_gate(&stub_gate("sys.exit(0)"));
    let src = fx.source("a.txt", TRAILERS);
    let marker = fx.p().join(".git/shadow-ran");
    // Untracked, made after the source commit; the python of the cut runs with the checkout as its directory.
    std::fs::write(
        fx.p().join("fnmatch.py"),
        format!(
            "open({:?}, 'w').write('ran')\n",
            marker.display().to_string()
        ),
    )
    .unwrap();
    let in_checkout = || {
        let mut c = python();
        c.current_dir(fx.p());
        c
    };
    fx.cut_to("slice/a", &[src], &gate_of(&fx), &in_checkout)
        .unwrap();
    assert!(!marker.exists(), "the checkout-local fnmatch.py ran");
}

/// The gate script of the base commit of `fx`, as a file the cut can be pointed at.
fn gate_of(fx: &Fx) -> std::path::PathBuf {
    let out = fx.p().join(".git/base_gate.py");
    std::fs::write(
        &out,
        g(
            fx.p(),
            &["show", &format!("{}:tools/ci_commit_gate.py", fx.base)],
        ),
    )
    .unwrap();
    out
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
