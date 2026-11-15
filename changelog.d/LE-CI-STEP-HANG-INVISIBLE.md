Bound Python CI jobs and browser test commands with step deadlines and a shared
file-backed GNU timeout runner, retaining diagnostics and failure status when a
child escapes the timeout process group (D-6054). Rust regressions pin every
pytest step's timeout and exercise escaped stdout holders. First slice: the
remaining non-Python CI jobs still need bounded logging and step deadlines.
