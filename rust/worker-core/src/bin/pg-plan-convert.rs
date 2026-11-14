//! JSON transport for plan conversion; offline keeper checks run in the CLI bridge.
use std::io::{self, Read};
fn main() {
    let mut input = String::new();
    let result = io::stdin()
        .read_to_string(&mut input)
        .map_err(|e| e.to_string())
        .and_then(|_| serde_json::from_str(&input).map_err(|e| format!("input: {e}")))
        .and_then(|request| pg_worker_core::plan_convert::convert(&request));
    match result {
        Ok(result) => println!("{}", serde_json::to_string(&result).unwrap()),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    }
}
