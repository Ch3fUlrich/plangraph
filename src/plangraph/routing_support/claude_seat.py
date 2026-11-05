"""Tool scope of a claude harness run (D-3835, D-3872: pytest on the review seat only).

Only the review role leaves plan mode. Its Bash is pytest on named paths: options
(--junit-xml, --basetemp) write outside the review; quotes and backslashes could hide one.
Repo settings stay unread: the diff under review must not widen its own seat.
"""

REVIEW_TOOLS = "Read,Grep,Glob,Bash(python3 -m pytest -q *)"
REVIEW_DENIED = ("Write,Edit,Bash(python3 -m pytest -q -*),Bash(python3 -m pytest -q * -*),"
                 "Bash(*\"*),Bash(*'*),Bash(*\\*)")


def claude_access_args(role: str) -> list[str]:
    if role != "review":
        return ["--permission-mode", "plan"]
    return ["--permission-mode", "default", "--setting-sources", "user",
            "--allowedTools", REVIEW_TOOLS, "--disallowedTools", REVIEW_DENIED]
