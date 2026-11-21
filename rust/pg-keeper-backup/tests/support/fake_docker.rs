//! A stand-in for `docker run postgres:17 <tool> ...` used by the CLI tests. It logs the
//! arguments, emulates the few tools the backup calls, and leaks `PGPASSWORD` into
//! stderr when told to, the way a real client error message could.

use std::io::Write;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Ok(path) = std::env::var("FAKE_DOCKER_LOG") {
        let mut log = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .unwrap();
        writeln!(log, "{}", args.join(" ")).unwrap();
    }
    let at = args.iter().position(|a| a == "postgres:17").expect("image") + 1;
    let tool = &args[at..];
    // The archive named by a `pg_restore` call is read through the `-v dir:/backup:ro` mount.
    let archive = args.windows(2).find(|w| w[0] == "-v").and_then(|w| {
        let dir = w[1].split(':').next()?;
        let file = tool_file(&args)?;
        std::fs::read(std::path::Path::new(dir).join(file.strip_prefix("/backup/")?)).ok()
    });
    let sql = tool
        .iter()
        .skip_while(|a| *a != "-c")
        .nth(1)
        .cloned()
        .unwrap_or_default();
    let fail = |what: &str| -> ! {
        eprintln!(
            "{what} (password {})",
            std::env::var("PGPASSWORD").unwrap_or_default()
        );
        std::process::exit(1);
    };
    match tool[0].as_str() {
        "pg_dump" => print!(
            "PGDMP seq={}",
            std::env::var("FAKE_DOCKER_SEQ").unwrap_or_else(|_| "19200".into())
        ),
        "pg_restore" if !archive.as_deref().is_some_and(|a| a.starts_with(b"PGDMP")) => {
            fail("pg_restore: input file does not appear to be a valid archive")
        }
        "pg_restore" if tool.contains(&"-l".to_owned()) => {
            for i in 0..43 {
                println!("{}; 1259 {} TABLE keeper t{i} keeper", 200 + i, 16000 + i);
                println!("{}; 0 {} TABLE DATA keeper t{i} keeper", 300 + i, 16000 + i);
            }
        }
        "pg_restore" => {
            // Loading an archive puts its seq into the scratch state.
            let text = String::from_utf8_lossy(archive.as_deref().unwrap_or_default()).into_owned();
            let seq = text.split("seq=").nth(1).unwrap_or("0").trim().to_owned();
            std::fs::write(scratch_state_path(), format!("43|{seq}\n")).unwrap();
        }
        "psql" if sql.starts_with("CREATE DATABASE") => {
            if std::env::var("FAKE_DOCKER_COLLIDE").is_ok() {
                fail("database already exists");
            }
        }
        "psql" if sql.starts_with("DROP DATABASE") => {
            let _ = std::fs::remove_file(scratch_state_path());
        }
        "psql" if sql.contains("pg_tables") => {
            match std::fs::read_to_string(scratch_state_path()) {
                Ok(state) => print!("{state}"),
                Err(_) => fail("scratch database holds no keeper schema"),
            }
        }
        "psql" if sql.contains("max(seq)") => println!(
            "{}",
            std::env::var("FAKE_DOCKER_SEQ").unwrap_or_else(|_| "19200".into())
        ),
        other => fail(&format!("unexpected tool {other}")),
    }
}

fn tool_file(args: &[String]) -> Option<&String> {
    args.iter().rev().find(|a| a.starts_with("/backup/"))
}

fn scratch_state_path() -> String {
    format!(
        "{}.scratch",
        std::env::var("FAKE_DOCKER_LOG").unwrap_or_else(|_| "/tmp/fake-docker".into())
    )
}
