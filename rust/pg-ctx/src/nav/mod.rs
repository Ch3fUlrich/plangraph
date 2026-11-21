//! One commit-stamped navigation index for CLI and context consumers.
mod git;
mod markdown;
mod query;
mod semantic;
mod syntax;

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};

pub type NavResult<T> = Result<T, Box<dyn std::error::Error>>;

#[derive(Clone, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct Location {
    pub path: String,
    pub start_line: usize,
    pub end_line: usize,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Symbol {
    pub name: String,
    pub qualified_name: String,
    pub kind: String,
    pub location: Location,
    pub signature: String,
    pub doc_comment: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Section {
    pub title: String,
    pub location: Location,
    terms: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Item {
    pub key: String,
    #[serde(default)]
    pub title: String,
    pub refs: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct RecentCommit {
    pub commit: String,
    pub subject: String,
}

#[derive(Debug, Default)]
pub struct BuildOptions {
    pub scip: Vec<ScipInput>,
    pub links: Option<PathBuf>,
    pub rules: Option<PathBuf>,
}

#[derive(Debug)]
pub struct ScipInput {
    pub file: PathBuf,
    pub prefix: String,
}

#[derive(Debug, Default, Deserialize, Serialize)]
struct Index {
    index_commit: String,
    tracked_paths: BTreeSet<String>,
    symbols: Vec<Symbol>,
    sections: Vec<Section>,
    items: Vec<Item>,
    semantic: semantic::Edges,
    scip_covered_files: BTreeSet<String>,
}

#[derive(Serialize)]
pub struct BuildReport {
    pub index_commit: String,
    pub index: PathBuf,
    pub symbols: usize,
    pub sections: usize,
    pub items: usize,
    pub scip_covered_files: usize,
}

pub use query::Navigation;
pub use semantic::{Definition as ScipDefinition, Reference as ScipReference, ScipSymbol};

/// Build from committed blobs and publish the completed index atomically.
pub fn build(cwd: &Path, options: &BuildOptions) -> NavResult<BuildReport> {
    let root = git::root(cwd)?;
    let commit = git::head(&root)?;
    let mut index = Index {
        index_commit: commit.clone(),
        ..Index::default()
    };
    let inputs = semantic::Inputs::read(&options.scip)?;
    let semantic_paths = inputs.paths();
    let mut source_maps = BTreeMap::new();
    let repo = gix::open_opts(&root, gix::open::Options::isolated())?;
    for (path, id) in git::files(&root, &commit)? {
        index.tracked_paths.insert(path.clone());
        let language = syntax::language(&path);
        let is_markdown = path.ends_with(".md");
        let extensionless = !path.rsplit('/').next().unwrap_or("").contains('.');
        if language.is_none() && !extensionless && !semantic_paths.contains(&path) {
            continue;
        }
        let blob = repo.find_blob(id)?;
        let source = match std::str::from_utf8(&blob.data) {
            Ok(source) => source,
            Err(_) if language.is_none() => continue,
            Err(error) => return Err(format!("{path}: {error}").into()),
        };
        if is_markdown {
            if !(markdown::is_rule(&path) && options.rules.is_some()) {
                index.add_markdown(&path, source)?;
            }
            continue;
        }
        let syntax = language
            .or_else(|| {
                extensionless
                    .then(|| syntax::shell_script(source))
                    .flatten()
            })
            .map(|language| {
                let tree = syntax::parse(&language, source)?;
                Ok::<_, Box<dyn std::error::Error>>(syntax::extract(&path, source, &tree))
            })
            .transpose()?;
        let is_semantic = semantic_paths.contains(&path);
        if is_semantic {
            source_maps.insert(
                path.clone(),
                semantic::SourceMap::new(source, syntax.as_ref(), index.symbols.len()),
            );
        }
        if let Some(syntax) = syntax {
            index
                .symbols
                .extend(syntax.definitions.into_iter().map(|def| def.symbol));
        }
    }
    if let Some(path) = &options.rules {
        index.add_markdown("router/DECISIONS.md", &fs::read_to_string(path)?)?;
    }
    if let Some(path) = &options.links {
        index.items = serde_json::from_reader(BufReader::new(File::open(path)?))?;
    }
    index.retain_section_links();
    index.scip_covered_files = source_maps.keys().cloned().collect();
    index.semantic = inputs.resolve(&source_maps)?;
    if git::head(&root)? != commit {
        return Err("HEAD changed during navigation index build; rebuild at the new HEAD".into());
    }
    let destination = git::default_index(&root)?;
    let parent = destination
        .parent()
        .ok_or("navigation index path has no parent")?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    {
        let mut writer = BufWriter::new(temporary.as_file_mut());
        serde_json::to_writer(&mut writer, &index)?;
        writer.flush()?;
    }
    temporary.persist(&destination)?;
    Ok(BuildReport {
        index_commit: commit,
        index: destination,
        symbols: index.symbols.len(),
        sections: index.sections.len(),
        items: index.items.len(),
        scip_covered_files: index.scip_covered_files.len(),
    })
}

impl Index {
    fn retain_section_links(&mut self) {
        let mut names = BTreeSet::new();
        for symbol in &self.symbols {
            names.insert(symbol.name.clone());
            names.insert(symbol.qualified_name.clone());
            names.insert(format!(
                "{}::{}",
                symbol.location.path, symbol.qualified_name
            ));
        }
        let keys: BTreeSet<_> = self.items.iter().map(|item| item.key.as_str()).collect();
        for section in &mut self.sections {
            section.terms.retain(|term| {
                self.tracked_paths.contains(term)
                    || names.contains(term)
                    || keys.contains(term.as_str())
                    || markdown::is_rule_id(term)
            });
        }
    }

    fn add_markdown(&mut self, path: &str, source: &str) -> NavResult<()> {
        let (headings, sections) = markdown::extract(path, source)?;
        self.symbols.extend(headings);
        if markdown::is_doc(path) {
            self.sections.extend(sections);
        }
        Ok(())
    }
}

/// Resolve an exact symbol, tracked path, or keeper key against the worktree index.
pub fn navigate(cwd: &Path, key: &str) -> NavResult<Navigation> {
    let root = git::root(cwd)?;
    let path = git::default_index(&root)?;
    let file = File::open(&path)
        .map_err(|error| format!("{}: {error}; run pg-ctx nav-build", path.display()))?;
    let index: Index = serde_json::from_reader(BufReader::new(file))?;
    query::lookup(index, &root, key)
}
