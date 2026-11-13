fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let arg = match args.as_slice() {
        [] => None,
        [a, b] if a == "--role" && pg_fleet::digest_text::safe(b) => Some(b.as_str()),
        _ => {
            eprintln!("usage: resume-digest-carried [--role ROLE]");
            std::process::exit(2)
        }
    };
    let mut s = pg_fleet::digest::State::load(
        pg_fleet::digest::Config::environment(),
        arg,
        &pg_fleet::digest::Local,
    );
    pg_fleet::digest_card::section(&mut s);
    println!("{}", pg_fleet::carried::section(&s).join("\n"));
}
