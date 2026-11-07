//! Fail-closed host pending configured HTTP transport support.
use og_memory::{
    adapters,
    io::{Error, Memory, Receipt, Snapshot, Transport},
    Capabilities, Validated,
};
use std::io::{self, BufRead, Read};
struct Unavailable;
impl Transport for Unavailable {
    fn read(&mut self, _: &str) -> Result<Snapshot, Error> {
        Err(Error::Outage)
    }
    fn commit(&mut self, _: &Validated, _: &str) -> Result<Receipt, Error> {
        Err(Error::Outage)
    }
}
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let mut m = Memory::new(Unavailable);
    let c = Capabilities::default();
    if args == ["mcp"] {
        for line in io::stdin().lock().lines() {
            let Ok(line) = line else {
                std::process::exit(1)
            };
            let Ok(request) = serde_json::from_str(&line) else {
                eprintln!("InvalidJsonRpc");
                std::process::exit(1)
            };
            if let Some(response) = adapters::mcp(&mut m, &c, request) {
                println!("{response}");
            }
        }
        return;
    }
    let mut body = String::new();
    if args.first().is_some_and(|s| s == "remember")
        && io::stdin().read_to_string(&mut body).is_err()
    {
        eprintln!("StdinUnavailable");
        std::process::exit(1);
    }
    let result = adapters::cli(&mut m, &c, &args, &body);
    let code = adapters::exit_code(&result);
    match result {
        Err(reason) => eprintln!("{reason}"),
        Ok(value) if args.first().is_some_and(|s| s == "recall") => println!(
            "saved\n{}\n\nretained-blocked\n{}",
            value["saved"], value["retained_blocked"]
        ),
        Ok(value) => println!("{value}"),
    }
    std::process::exit(code);
}
