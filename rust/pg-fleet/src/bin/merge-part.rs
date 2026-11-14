fn main() {
    std::process::exit(pg_fleet::merge_part::main(
        &std::env::args().skip(1).collect::<Vec<_>>(),
    ));
}
