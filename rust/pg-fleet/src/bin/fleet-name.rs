fn main() {
    std::process::exit(pg_fleet::fleet_name::main(
        &std::env::args().skip(1).collect::<Vec<_>>(),
    ));
}
