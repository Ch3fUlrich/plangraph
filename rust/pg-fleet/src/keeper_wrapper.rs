//! Compatibility output policy: JSON is never truncated.
pub fn output(text: &str, json: bool) -> String {
    if json || text.chars().count() <= 3000 {
        text.to_owned()
    } else {
        format!(
            "{}\n[truncated; use --json for full output]\n",
            text.chars().take(3000).collect::<String>()
        )
    }
}
pub fn main(args: &[String]) -> i32 {
    let program = std::env::var("KC_PROGRAM").unwrap_or("pg".into());
    let mut cmd = crate::process::command(&program);
    cmd.args(args);
    match crate::process::capture(&mut cmd, 120) {
        Ok(out) => {
            print!(
                "{}",
                output(&out.stdout, args.iter().any(|a| a == "--json"))
            );
            eprint!("{}", out.stderr);
            out.code
        }
        Err(e) => {
            eprintln!("kc: {e}");
            3
        }
    }
}
