fn main() {
    std::process::exit(pg_fleet::go_gate::main(
        &std::env::args().skip(1).collect::<Vec<_>>(),
    ));
}
