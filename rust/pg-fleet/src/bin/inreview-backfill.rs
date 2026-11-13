fn main() {
    std::process::exit(pg_fleet::backfill_cli::main(
        &std::env::args().skip(1).collect::<Vec<_>>(),
    ));
}
