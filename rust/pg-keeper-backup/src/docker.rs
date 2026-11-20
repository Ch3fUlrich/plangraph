//! `Pg` over `docker run postgres:17`. The connection comes from the PG* variables of
//! this process, handed to the container by NAME (`-e PGPASSWORD`): no value is ever
//! put on a command line, in a log or in an error message.

use crate::pg::{check_ident, redact, Pg, ScratchState};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;

pub const PG_ENV: [&str; 6] = [
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
    "PGDATABASE",
    "PGSSLMODE",
];

pub struct Docker {
    pub image: String,
}

/// Arguments of `docker run`; the dump directory is mounted read-only at `/backup`.
pub fn run_args(image: &str, mount: Option<&Path>, tool: &[&str]) -> Vec<String> {
    let mut args: Vec<String> = ["run", "--rm", "-i", "--network", "host"]
        .map(String::from)
        .into();
    for name in PG_ENV {
        args.extend(["-e".to_owned(), name.to_owned()]);
    }
    if let Some(dir) = mount {
        args.extend(["-v".to_owned(), format!("{}:/backup:ro", dir.display())]);
    }
    args.push(image.to_owned());
    args.extend(tool.iter().map(|s| (*s).to_owned()));
    args
}

fn secrets() -> Vec<String> {
    std::env::var("PGPASSWORD").into_iter().collect()
}

fn backup_path(dump: &Path) -> Result<(PathBuf, String), String> {
    let dir = dump
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let dir = dir.canonicalize().map_err(|e| format!("dump dir: {e}"))?;
    let name = dump
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or("dump name")?;
    Ok((dir, format!("/backup/{name}")))
}

impl Docker {
    /// Run a tool; stdout goes to `out`, stderr comes back redacted on failure.
    fn exec(&self, mount: Option<&Path>, tool: &[&str], out: &mut dyn Write) -> Result<(), String> {
        let mut child = Command::new("docker")
            .args(run_args(&self.image, mount, tool))
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("docker: {e}"))?;
        let mut stderr = child.stderr.take().ok_or("stderr")?;
        let reader = thread::spawn(move || {
            let mut text = String::new();
            let _ = stderr.read_to_string(&mut text);
            text
        });
        let copied = std::io::copy(&mut child.stdout.take().ok_or("stdout")?, out);
        let status = child.wait().map_err(|e| format!("docker: {e}"))?;
        let text = reader.join().unwrap_or_default();
        if !status.success() {
            return Err(format!(
                "{} failed ({status}): {}",
                tool[0],
                redact(text.trim(), &secrets())
            ));
        }
        copied.map(|_| ()).map_err(|e| format!("{}: {e}", tool[0]))
    }

    fn query(&self, db: Option<&str>, sql: &str) -> Result<String, String> {
        let mut tool = vec!["psql", "-X", "-v", "ON_ERROR_STOP=1", "-At"];
        if let Some(db) = db {
            tool.extend(["-d", db]);
        }
        tool.extend(["-c", sql]);
        let mut buf = Vec::new();
        self.exec(None, &tool, &mut buf)?;
        Ok(String::from_utf8_lossy(&buf).trim().to_owned())
    }
}

impl Pg for Docker {
    fn max_seq(&self, schema: &str) -> Result<u64, String> {
        let sql = format!(
            "SELECT COALESCE(max(seq), 0) FROM \"{}\".events",
            check_ident(schema)?
        );
        self.query(None, &sql)?
            .parse()
            .map_err(|_| "max(seq) is not a number".to_owned())
    }

    fn dump(&self, schema: &str, out: &mut dyn Write) -> Result<(), String> {
        self.exec(
            None,
            &["pg_dump", "-Fc", "--no-owner", "-n", check_ident(schema)?],
            out,
        )
    }

    fn list(&self, dump: &Path) -> Result<String, String> {
        let (dir, file) = backup_path(dump)?;
        let mut buf = Vec::new();
        self.exec(Some(&dir), &["pg_restore", "-l", &file], &mut buf)?;
        Ok(String::from_utf8_lossy(&buf).into_owned())
    }

    fn restore_scratch(&self, dump: &Path, scratch: &str) -> Result<(), String> {
        let (dir, file) = backup_path(dump)?;
        self.query(
            None,
            &format!("CREATE DATABASE \"{}\"", check_ident(scratch)?),
        )?;
        let tool = [
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "-d",
            scratch,
            &file,
        ];
        self.exec(Some(&dir), &tool, &mut std::io::sink())
    }

    fn scratch_state(&self, scratch: &str, schema: &str) -> Result<ScratchState, String> {
        let schema = check_ident(schema)?;
        let sql = format!(
            "SELECT (SELECT count(*) FROM pg_tables WHERE schemaname = '{schema}'), \
             (SELECT COALESCE(max(seq), 0) FROM \"{schema}\".events)"
        );
        let text = self.query(Some(check_ident(scratch)?), &sql)?;
        let (tables, seq) = text.split_once('|').ok_or("scratch state")?;
        Ok(ScratchState {
            tables: tables.parse().map_err(|_| "scratch table count")?,
            max_seq: seq.parse().map_err(|_| "scratch max seq")?,
        })
    }

    fn drop_scratch(&self, scratch: &str) -> Result<(), String> {
        self.query(
            None,
            &format!("DROP DATABASE IF EXISTS \"{}\"", check_ident(scratch)?),
        )
        .map(|_| ())
    }
}
