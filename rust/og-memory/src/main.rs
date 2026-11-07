//! Fail-closed host pending configured HTTP transport support.
use og_memory::{
    adapters,
    io::{Error, Memory, Receipt, Snapshot, Transport},
    Capabilities, Validated,
};
use std::io::{self, BufRead, Read};
const MAX_INPUT: u64 = 1024 * 1024;
struct Unavailable;
impl Transport for Unavailable {
    fn read(&mut self, _: &str) -> Result<Snapshot, Error> {
        Err(Error::Outage)
    }
    fn commit(&mut self, _: &Validated, _: &str) -> Result<Receipt, Error> {
        Err(Error::Unavailable)
    }
}
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let mut m = Memory::new(Unavailable);
    let c = Capabilities::default();
    if args == ["mcp"] {
        let mut stdin = io::stdin().lock();
        loop {
            let mut line = Vec::new();
            let read = stdin
                .by_ref()
                .take(MAX_INPUT + 1)
                .read_until(b'\n', &mut line);
            match read {
                Ok(0) => break,
                Err(_) => {
                    eprintln!("StdinUnavailable");
                    std::process::exit(1);
                }
                _ => {}
            }
            if line.len() as u64 > MAX_INPUT {
                println!("{}", parse_error("InputTooLarge"));
                if line.last() != Some(&b'\n') && drain_line(&mut stdin).is_err() {
                    eprintln!("StdinUnavailable");
                    std::process::exit(1);
                }
                continue;
            }
            match serde_json::from_slice(&line) {
                Ok(request) => {
                    if let Some(response) = adapters::mcp(&mut m, &c, request) {
                        println!("{response}");
                    }
                }
                Err(_) => println!("{}", parse_error("InvalidJsonRpc")),
            }
        }
        return;
    }
    let mut bytes = Vec::new();
    if args.first().is_some_and(|s| s == "remember") {
        if io::stdin()
            .take(MAX_INPUT + 1)
            .read_to_end(&mut bytes)
            .is_err()
        {
            eprintln!("StdinUnavailable");
            std::process::exit(1);
        }
        if bytes.len() as u64 > MAX_INPUT {
            eprintln!("InputTooLarge");
            std::process::exit(1);
        }
    }
    let Ok(body) = String::from_utf8(bytes) else {
        eprintln!("InvalidEnvelope");
        std::process::exit(1);
    };
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
fn parse_error(message: &str) -> serde_json::Value {
    serde_json::json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":message}})
}

fn drain_line(input: &mut impl BufRead) -> io::Result<()> {
    loop {
        let chunk = input.fill_buf()?;
        if chunk.is_empty() {
            return Ok(());
        }
        let end = chunk.iter().position(|b| *b == b'\n');
        let consumed = end.map_or(chunk.len(), |i| i + 1);
        input.consume(consumed);
        if end.is_some() {
            return Ok(());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct InterruptedLine {
        remaining: usize,
    }
    impl Read for InterruptedLine {
        fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
            let chunk = self.fill_buf()?;
            let count = output.len().min(chunk.len());
            output[..count].copy_from_slice(&chunk[..count]);
            self.consume(count);
            Ok(count)
        }
    }
    impl BufRead for InterruptedLine {
        fn fill_buf(&mut self) -> io::Result<&[u8]> {
            if self.remaining == 0 {
                Err(io::Error::other("injected stdin failure"))
            } else {
                Ok(b"x")
            }
        }
        fn consume(&mut self, count: usize) {
            self.remaining -= count;
        }
    }
    #[test]
    fn oversized_line_drain_propagates_read_error() {
        let mut input = InterruptedLine {
            remaining: MAX_INPUT as usize + 4,
        };
        let mut line = Vec::new();
        (&mut input)
            .take(MAX_INPUT + 1)
            .read_until(b'\n', &mut line)
            .unwrap();
        assert!(line.len() as u64 > MAX_INPUT);
        assert_eq!(
            drain_line(&mut input).unwrap_err().kind(),
            io::ErrorKind::Other
        );
    }
}
