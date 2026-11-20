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
        "pg_dump" => print!("PGDMP-fake"),
        "pg_restore" if tool.contains(&"-l".to_owned()) => {
            for i in 0..43 {
                println!("{}; 1259 {} TABLE keeper t{i} keeper", 200 + i, 16000 + i);
                println!("{}; 0 {} TABLE DATA keeper t{i} keeper", 300 + i, 16000 + i);
            }
        }
        "pg_restore" => {}
        "psql" if sql.starts_with("CREATE DATABASE") => {
            if std::env::var("FAKE_DOCKER_COLLIDE").is_ok() {
                fail("database already exists");
            }
        }
        "psql" if sql.starts_with("DROP DATABASE") => {}
        "psql" if sql.contains("pg_tables") => println!("43|19200"),
        "psql" if sql.contains("max(seq)") => println!("19200"),
        other => fail(&format!("unexpected tool {other}")),
    }
}
