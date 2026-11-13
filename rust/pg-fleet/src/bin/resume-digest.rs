fn main() {
    std::process::exit(pg_fleet::digest::main(
        &std::env::args().skip(1).collect::<Vec<_>>(),
    ));
}
