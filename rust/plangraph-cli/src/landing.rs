use crate::feeder::{Env, SlowKeeper, Transport};
use serde_json::Value;
use std::io;

pub(crate) fn deliver_from_env(body: Value) -> Result<Value, String> {
    let env = crate::env::load(&crate::env::default_path()?, std::env::vars())?;
    SlowKeeper {
        env: &env,
        max_time_secs: 30,
    }
    .call("land", Some(body))
}

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
    let keeper = SlowKeeper {
        env,
        max_time_secs: 30,
    };
    match args {
        [command] if command == "journal" => {
            let body =
                serde_json::from_reader(io::stdin().lock()).map_err(|_| "invalid landing JSON")?;
            keeper.call("land", Some(body))
        }
        [command, rest @ ..] if command == "ci-import" => crate::landing_ci::import(&keeper, rest),
        _ => Err("usage: plangraph landing journal|ci-import".into()),
    }
}
