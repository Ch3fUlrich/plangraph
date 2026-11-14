fn main() {
    let kind = std::env::args().nth(1).unwrap_or_default();
    if !["pgk", "evidence"].contains(&kind.as_str()) {
        std::process::exit(1)
    }
    match pg_fleet::merge_paths::lookup(&kind) {
        Ok(p) => println!("{}", pg_fleet::paths::display(&p)),
        Err(e) => {
            eprintln!("STOP: {e}");
            std::process::exit(6)
        }
    }
}
