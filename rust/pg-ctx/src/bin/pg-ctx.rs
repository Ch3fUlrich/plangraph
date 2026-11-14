fn main() {
    match pg_ctx::hygiene_cli::execute(&std::env::args().skip(1).collect::<Vec<_>>()) {
        Ok(output) => println!("{output}"),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    }
}
