pub fn count(text: &str) -> String {
    let row = crate::handoff::re(r"^\| *\d+ *\|");
    let nonblocking = crate::handoff::re(r"(?i)NOT BLOCKING|nothing waits");
    let priority = crate::handoff::re(r"^\| *[01] *\|");
    let (mut total, mut answers, mut tasks, mut nonblock, mut urgent) = (0, 0, 0, 0, 0);
    for line in text.lines().filter(|s| row.is_match(s)) {
        total += 1;
        match line.trim_matches('|').split('|').nth(2).map(str::trim) {
            Some("ANSWER") => answers += 1,
            Some("TASK") => tasks += 1,
            _ => {}
        }
        nonblock += usize::from(nonblocking.is_match(line));
        urgent += usize::from(priority.is_match(line));
    }
    format!("operator queue: {total} open ({answers} answers, {tasks} tasks); {} block agent work, {nonblock} block nothing; prio-0/1: {urgent}",total-nonblock)
}

pub fn main() -> i32 {
    let path = crate::data_home().join("QUESTIONS.md");
    match std::fs::read_to_string(&path) {
        Ok(text) => {
            println!("{}", count(&text));
            0
        }
        Err(e) => {
            eprintln!("question-count: {}: {e}", path.display());
            1
        }
    }
}
