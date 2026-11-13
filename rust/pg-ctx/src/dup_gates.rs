//! Read-only capability ownership checks; see docs/dup-gates.md.
use regex::Regex;
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Capability {
    pub name: String,
    pub canonical: String,
    pub language: String,
    pub forbidden_defs: Vec<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Registry {
    capability: Vec<Capability>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Baseline {
    files: BTreeMap<String, usize>,
}
pub fn parse(text: &str) -> Result<Vec<Capability>, String> {
    let registry: Registry = toml::from_str(text).map_err(|e| format!("capabilities.toml: {e}"))?;
    let mut names = BTreeSet::new();
    for c in &registry.capability {
        if c.canonical.trim().is_empty() {
            return Err(format!("{}: missing canonical", c.name));
        }
        extension(&c.language)?;
        if c.name.is_empty() || !names.insert(&c.name) {
            return Err(format!("duplicate/empty name: {}", c.name));
        }
    }
    Ok(registry.capability)
}
fn extension(language: &str) -> Result<&str, String> {
    match language {
        "python" => Ok("py"),
        "rust" => Ok("rs"),
        "javascript" => Ok("js"),
        "typescript" => Ok("ts"),
        _ => Err(format!("unknown language: {language}")),
    }
}
fn read(path: &Path) -> Result<String, String> {
    fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))
}
fn files(root: &Path, out: &mut Vec<PathBuf>) -> Result<(), String> {
    for entry in fs::read_dir(root).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let kind = entry.file_type().map_err(|e| e.to_string())?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if kind.is_dir()
            && !name.starts_with('.')
            && !matches!(
                name.as_ref(),
                "target" | "node_modules" | "vendor" | "third_party" | "__pycache__" | "venv"
            )
        {
            files(&entry.path(), out)?;
        } else if kind.is_file() {
            out.push(entry.path());
        }
    }
    Ok(())
}
fn module(path: &str) -> String {
    path.trim_start_matches("src/")
        .replace('/', ".")
        .replace("::", ".")
}
fn import_module(import: &str, file: &str) -> String {
    if !import.starts_with('.') {
        return module(import);
    }
    let mut parts: Vec<_> = file.trim_start_matches("src/").split('/').collect();
    for _ in 0..import.chars().take_while(|c| *c == '.').count() {
        parts.pop();
    }
    let base = parts.join(".");
    format!("{base}.{}", import.trim_start_matches('.'))
        .trim_end_matches('.')
        .to_string()
}
pub struct Violation {
    pub file: String,
    pub line: usize,
    pub capability: String,
}
pub fn scan(root: &Path, capabilities: &[Capability]) -> Result<Vec<Violation>, String> {
    let mut paths = Vec::new();
    files(root, &mut paths)?;
    paths.sort();
    let mut hits = Vec::new();
    for c in capabilities {
        let ext = extension(&c.language)?;
        let canonical = c.canonical.replace("::", "/").replace('.', "/") + "." + ext;
        let canonical = [canonical.clone(), format!("src/{canonical}")]
            .into_iter()
            .find(|p| root.join(p).is_file())
            .ok_or_else(|| format!("{}: canonical module missing", c.name))?;
        let escaped: Vec<_> = c.forbidden_defs.iter().map(|n| regex::escape(n)).collect();
        let names = escaped.join("|");
        let definition = Regex::new(&format!(
            r"(?m)^[\t ]*(?:(?:async|pub(?:\([^)]*\))?|unsafe|const|export|default)\s+)*(?:def|fn|function)\s+({names})\s*(?:<[^>]*>)?\s*\("
        ))
        .map_err(|e| e.to_string())?;
        let symbol = Regex::new(&format!(r"\b(?:{names})\b")).map_err(|e| e.to_string())?;
        let imports = Regex::new(match c.language.as_str() {
            "python" => r"(?m)^[\t ]*from\s+([.\w]+)\s+import\s+(\([^)]*\)|[^\r\n;#]+)|^[\t ]*import\s+([.\w]+)",
            "rust" => r"(?m)^[\t ]*(?:pub\s+)?use\s+([\w:]+)::([^;]+);",
            _ => r#"(?m)^[\t ]*import\s+([^;]+?)\s+from\s+["']([^"']+)["']"#,
        })
        .map_err(|e| e.to_string())?;
        for path in &paths {
            let suffix = path.extension().and_then(|s| s.to_str());
            if suffix != Some(ext) && !(suffix.is_none() && ext == "py") {
                continue;
            }
            let rel = path.strip_prefix(root).unwrap();
            let file = rel.to_string_lossy().replace('\\', "/");
            let text = read(path)?;
            let script =
                text.starts_with("#!") && text.lines().next().unwrap_or("").contains("python");
            if suffix.is_none() && !script {
                continue;
            }
            let mut positions: BTreeSet<_> = definition
                .captures_iter(&text)
                .filter(|_| file != canonical)
                .map(|m| m.get(1).unwrap().start())
                .collect();
            for m in imports.captures_iter(&text) {
                let (source, imported) = if let Some(direct) = m.get(3) {
                    direct.as_str().rsplit_once('.').unwrap_or(("", ""))
                } else if matches!(c.language.as_str(), "javascript" | "typescript") {
                    (m[2].trim(), m[1].trim())
                } else {
                    (m[1].trim(), m[2].trim())
                };
                let source = import_module(source, &file);
                let local = file
                    .rsplit_once('/')
                    .map(|(dir, _)| format!("{}.{}", module(dir), source));
                let owner = module(&c.canonical);
                if symbol.is_match(imported) && source != owner && local.as_deref() != Some(&owner)
                {
                    positions.insert(m.get(0).unwrap().start());
                }
            }
            for position in positions {
                hits.push(Violation {
                    file: file.clone(),
                    line: text[..position].bytes().filter(|b| *b == b'\n').count() + 1,
                    capability: c.name.clone(),
                });
            }
        }
    }
    hits.sort_by(|a, b| (&a.file, a.line, &a.capability).cmp(&(&b.file, b.line, &b.capability)));
    Ok(hits)
}
/// Reference must be the trusted base revision's baseline; counts may only shrink.
pub fn check(root: &Path, reference: &str) -> Result<(), String> {
    let caps = parse(&read(&root.join("capabilities.toml"))?)?;
    let baseline = |text: &str| -> Result<BTreeMap<String, usize>, String> {
        let b: Baseline = toml::from_str(text).map_err(|e| format!("baseline: {e}"))?;
        if b.files.values().any(|n| *n == 0) {
            return Err("baseline: zero count; remove entry".into());
        }
        Ok(b.files)
    };
    let proposed = baseline(&read(&root.join("capabilities-baseline.toml"))?)?;
    let base = baseline(reference)?;
    let mut errors = Vec::new();
    for (file, count) in &proposed {
        if *count > base.get(file).copied().unwrap_or(0) {
            errors.push(format!("{file}:1: baseline may only shrink"));
        }
    }
    let mut actual = BTreeMap::new();
    for hit in scan(root, &caps)? {
        let count = actual.entry(hit.file.clone()).or_insert(0);
        *count += 1;
        if *count > proposed.get(&hit.file).copied().unwrap_or(0) {
            let Violation {
                file,
                line,
                capability,
            } = hit;
            errors.push(format!("{file}:{line}: non-canonical {capability}"));
        }
    }
    for (file, count) in proposed {
        if actual.get(&file).copied().unwrap_or(0) < count {
            errors.push(format!("{file}:1: stale baseline entry"));
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("\n"))
    }
}
