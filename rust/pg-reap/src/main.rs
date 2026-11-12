use std::io::{self, Write};

fn run() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::args().skip(1).collect::<Vec<_>>() != ["report"] {
        return Err("usage: pg-reap report < repositories.json".into());
    }
    let repos: Vec<pg_reap::Repository> = serde_json::from_reader(io::stdin().lock())?;
    let output = pg_reap::report(&repos)?;
    io::stdout().lock().write_all(output.as_bytes())?;
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("pg-reap: {error}");
        std::process::exit(2);
    }
}
