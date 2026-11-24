use pg_keeper_backup::{docker::Docker, names, restore_test, run_dump, Verify, KEEP};
use std::path::PathBuf;

const USAGE: &str = "usage: pg-keeper-backup dump --dir DIR [--verify] [--tables N] [--schema keeper] [--keep 14] [--image postgres:17] [--docker PATH]\n       \
pg-keeper-backup restore-test --file DUMP [--schema keeper] [--tables N] [--image postgres:17]\n\
The connection comes from the libpq environment variables (see docs/keeper-backup.md).";

struct Opts {
    dir: Option<PathBuf>,
    file: Option<PathBuf>,
    schema: String,
    keep: usize,
    tables: Option<usize>,
    image: String,
    verify: bool,
    docker: String,
}

fn parse(args: &[String]) -> Result<Opts, String> {
    let mut o = Opts {
        dir: None,
        file: None,
        schema: "keeper".into(),
        keep: KEEP,
        tables: None,
        image: "postgres:17".into(),
        verify: false,
        docker: "docker".into(),
    };
    let mut it = args.iter();
    while let Some(flag) = it.next() {
        if flag == "--verify" {
            o.verify = true;
            continue;
        }
        let value = it.next().ok_or_else(|| format!("{flag} needs a value"))?;
        let number = |v: &str| {
            v.parse::<usize>()
                .map_err(|_| format!("{flag}: not a number"))
        };
        match flag.as_str() {
            "--dir" => o.dir = Some(value.into()),
            "--file" => o.file = Some(value.into()),
            "--schema" => o.schema = value.clone(),
            "--docker" => o.docker = value.clone(),
            "--image" => o.image = value.clone(),
            "--keep" => o.keep = number(value)?.max(1),
            "--tables" => o.tables = Some(number(value)?),
            _ => return Err(format!("unknown flag {flag}")),
        }
    }
    Ok(o)
}

fn run() -> Result<String, String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (cmd, rest) = args.split_first().ok_or(USAGE)?;
    let o = parse(rest)?;
    let pg = Docker {
        program: o.docker.clone(),
        image: o.image.clone(),
    };
    match cmd.as_str() {
        "dump" => {
            let dir = o.dir.ok_or("dump needs --dir")?;
            let verify = o.verify.then_some(Verify {
                tables: o.tables,
                min_seq: names::MIN_SEQ,
            });
            let done = run_dump(
                &pg,
                &dir,
                &o.schema,
                o.keep,
                names::now_secs(),
                verify.as_ref(),
            )?;
            Ok(format!(
                "{}\npruned {}",
                done.entry.line(),
                done.pruned.len()
            ))
        }
        "restore-test" => {
            let file = o.file.ok_or("restore-test needs --file")?;
            let r = restore_test(&pg, &file, &o.schema, o.tables, names::MIN_SEQ)?;
            Ok(format!("restore ok: seq {} tables {}", r.seq, r.tables))
        }
        _ => Err(USAGE.into()),
    }
}

fn main() {
    match run() {
        Ok(text) => println!("{text}"),
        Err(error) => {
            eprintln!("pg-keeper-backup: {error}");
            std::process::exit(2);
        }
    }
}
