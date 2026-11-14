#[test]
fn table_counts_match_legacy_regexes() {
    for (text,expected) in [
        ("", "operator queue: 0 open (0 answers, 0 tasks); 0 block agent work, 0 block nothing; prio-0/1: 0"),
        ("| Priority | Kind | Type |\n|---|---|---|\n| 0 | x | ANSWER | blocking |\n| 1 | y | TASK | NOT BLOCKING |\n| 2 | z | TASK | nothing waits |\n| 3 | x | OTHER | blocked |\nignored\n", "operator queue: 4 open (1 answers, 2 tasks); 2 block agent work, 2 block nothing; prio-0/1: 2"),
        (" | 0 | x | TASK |\n| 10 | x | ANSWER |\n| 01 | x | TASK | not blocking |\n| 9 | malformed |\n", "operator queue: 3 open (1 answers, 1 tasks); 2 block agent work, 1 block nothing; prio-0/1: 0"),
    ] { assert_eq!(pg_fleet::question_count::count(text),expected); }
}
