fn main() {
    match std::env::args().nth(1).as_deref() {
        Some("witness") => println!("witness"),
        Some("fail") => std::process::exit(1),
        Some("sleep") => std::thread::sleep(std::time::Duration::from_secs(2)),
        _ => {}
    }
}
