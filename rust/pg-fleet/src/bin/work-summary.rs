fn main() {
    std::process::exit(pg_fleet::summary::main(
        &std::env::args().skip(1).collect::<Vec<_>>(),
    ));
}
