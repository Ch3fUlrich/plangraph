use crate::feeder::{Env, SlowKeeper, Transport};
use serde_json::Value;
use std::io;

/// Send a landing journal request through the authenticated keeper transport.
pub fn run(env: &Env, args: &[String]) -> i32 {
    match execute(env, args) {
        Ok(row) => {
            println!("{row}");
            0
        }
        Err(error) => {
            eprintln!("landing: {error}");
            1
        }
    }
}

fn execute(env: &Env, args: &[String]) -> Result<Value, String> {
    if args != ["journal"] {
        return Err("usage: plangraph landing journal < request.json".into());
    }
    let body = serde_json::from_reader(io::stdin().lock()).map_err(|_| "invalid landing JSON")?;
    SlowKeeper {
        env,
        max_time_secs: 30,
    }
    .call("land", Some(body))
}
