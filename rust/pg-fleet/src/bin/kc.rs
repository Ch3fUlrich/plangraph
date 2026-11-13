fn main() {
    std::process::exit(pg_fleet::keeper_wrapper::main(
        &std::env::args().skip(1).collect::<Vec<_>>(),
    ));
}
