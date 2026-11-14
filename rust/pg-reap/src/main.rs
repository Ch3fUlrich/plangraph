use std::io::{self, Write};

fn run() -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(target_os = "linux")]
    if std::env::args().nth(1).as_deref() == Some("orphans") {
        let path = std::env::args()
            .nth(2)
            .ok_or("usage: pg-reap orphans EVIDENCE.json")?;
        if std::env::args().len() != 3 {
            return Err("unexpected argument".into());
        }
        let evidence: Vec<pg_reap::orphans::Evidence> =
            serde_json::from_reader(std::fs::File::open(path)?)?;
        let mut failed = false;
        for row in evidence {
            match pg_reap::orphans::reap(&row) {
                Ok(true) => println!("reaped {} {}", row.pid, row.start_ticks),
                Ok(false) => println!("unverified_or_gone {}", row.pid),
                Err(_) => {
                    eprintln!("kill_incomplete {}", row.pid);
                    failed = true;
                }
            }
        }
        return if failed {
            Err("orphan sweep incomplete".into())
        } else {
            Ok(())
        };
    }
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
