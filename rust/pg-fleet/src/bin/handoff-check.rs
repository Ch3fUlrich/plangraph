fn main() {
    std::process::exit(pg_fleet::handoff::main(
        &std::env::args().skip(1).collect::<Vec<_>>(),
    ));
}
