//! The gate path list of a brief: read from `tools/ci_commit_gate.py` of the checked-out tree.
//!
//! `GATE_PATHS` lives only in that file. A brief never carries a copied list: the builder reads
//! the tuple of the writer's own checkout, so a path added to the gate is in the next brief.

use std::path::Path;

const GATE_FILE: &str = "tools/ci_commit_gate.py";

/// The string entries of the `GATE_PATHS` tuple of `source`, comments skipped.
pub fn parse(source: &str) -> Result<Vec<String>, String> {
    let mut lines = source
        .lines()
        .skip_while(|line| !line.starts_with("GATE_PATHS"));
    let first = lines.next().ok_or("GATE_PATHS not found")?;
    let mut paths = Vec::new();
    for line in std::iter::once(first).chain(lines) {
        let code = line.split_once('#').map_or(line, |(code, _)| code);
        let code = code.split_once('=').map_or(code, |(_, tuple)| tuple);
        let mut rest = code;
        while let Some(open) = rest.find(['\'', '"']) {
            let quote = &rest[open..=open];
            let body = &rest[open + 1..];
            let Some(close) = body.find(quote) else { break };
            paths.push(body[..close].to_owned());
            rest = &body[close + 1..];
        }
        if code.trim() == ")" {
            break;
        }
    }
    if paths.is_empty() {
        return Err("GATE_PATHS has no entries".into());
    }
    Ok(paths)
}

/// The gate paths of the checkout at `worktree`; `None` when it has no gate file (no list to print).
pub fn read(worktree: &str) -> Result<Option<Vec<String>>, String> {
    let file = Path::new(worktree).join(GATE_FILE);
    match std::fs::read_to_string(&file) {
        Ok(source) => parse(&source).map(Some),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!("cannot read {GATE_FILE}: {error}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_tuple_is_read_without_comments_or_neighbours() {
        let source = "OTHER = (\n    'x/*',\n)\n\n#: doc\nGATE_PATHS: tuple[str, ...] = (\n    'a/b.py*',\n    # a note: 'not/this'\n    \"c/*\",  # trailing 'nor/this'\n    '.d',\n)\n\nAFTER = ('z',)\n";
        assert_eq!(parse(source).unwrap(), ["a/b.py*", "c/*", ".d"]);
    }

    #[test]
    fn a_file_without_the_tuple_is_an_error() {
        assert!(parse("X = 1\n").is_err());
    }

    #[test]
    fn the_list_equals_what_python_reads_from_this_tree() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let ours = read(root.to_str().unwrap()).unwrap().unwrap();
        let out = crate::test_python::command()
            .current_dir(root.join("tools"))
            .args([
                "-c",
                "import ci_commit_gate as g; print(chr(10).join(g.GATE_PATHS))",
            ])
            .output()
            .unwrap();
        assert!(out.status.success(), "{out:?}");
        let python: Vec<_> = String::from_utf8(out.stdout)
            .unwrap()
            .lines()
            .map(str::to_owned)
            .collect();
        assert_eq!(ours, python);
    }
}
