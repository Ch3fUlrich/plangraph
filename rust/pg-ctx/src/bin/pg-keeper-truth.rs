use pg_ctx::keeper_truth::collect;
use std::path::Path;

fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let result = match args.as_slice() {
        [verb, repo, reference] if verb == "index" => collect(Path::new(repo), reference, true)
            .and_then(|index| {
                serde_json::to_string(&index).map_err(|_| "index encoding failed".into())
            }),
        _ => Err("usage: pg-keeper-truth index REPOSITORY DEFAULT-REF".into()),
    };
    match result {
        Ok(output) => println!("{output}"),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    }
}
