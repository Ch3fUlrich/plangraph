pub fn main(args: &[String]) -> i32 {
    let opts = match crate::cli::options(args, &["--since", "--project"], &["--help", "-h"]) {
        Ok(o) => o,
        Err(e) => return crate::cli::error("work-summary", &e),
    };
    if opts.contains_key("--help") || opts.contains_key("-h") {
        print!("{}", crate::cli::help("work-summary"));
        return 0;
    }
    let mut c = crate::summary::Config::environment();
    if let Some(s) = opts.get("--since") {
        c.since = s.clone();
    }
    c.project = opts.get("--project").cloned();
    match crate::summary::build(&c, &crate::digest::Local) {
        Ok(lines) => {
            println!("{}", lines.join("\n"));
            0
        }
        Err(e) => {
            eprintln!("work-summary: {e}");
            2
        }
    }
}
