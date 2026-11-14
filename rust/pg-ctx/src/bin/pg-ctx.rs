fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let result = match args.as_slice() {
        [verb, tail @ ..] if verb == "backfill-plan" => pg_ctx::backfill_cli::execute(tail),
        _ => pg_ctx::hygiene_cli::execute(&args),
    };
    match result {
        Ok(output) => println!("{output}"),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    }
}
