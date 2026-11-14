use pg_ctx::mirror::{Alarm, Mirror};
use std::path::Path;
fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() == 3 && args[0] == "--fetch-one" {
        Mirror::run_fetch_worker(Path::new(&args[1]), Path::new(&args[2]))?;
        return Ok(());
    }
    if args.len() != 2 {
        return Err("usage: pg-repo-mirror REPOS.json MIRROR_ROOT".into());
    }
    let mirror = Mirror::from_repos(Path::new(&args[0]), Path::new(&args[1]))?;
    let mut failed = false;
    for (repo, result) in mirror.fetch_all() {
        let row = match result {
            Ok(watermark) => serde_json::json!({"repo":repo,"watermark":watermark,"alarm":null}),
            Err(alarm) => {
                failed = true;
                serde_json::json!({"repo":repo,"alarm":match alarm {
                    Alarm::ForcePush => "force_push", Alarm::FetchFailed => "fetch_failed", Alarm::FetchTimedOut => "fetch_timed_out"
                }})
            }
        };
        println!("{row}");
    }
    if failed {
        return Err("one or more mirrors failed".into());
    }
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("pg-repo-mirror: {error}");
        std::process::exit(1);
    }
}
