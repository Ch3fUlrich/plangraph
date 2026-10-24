"""Shared planner/keeper/readiness check-input path contract cases."""

CHECK_PATH_CASES = [
    ("tests/a.py", True), ("./tests\\a.py", True), ("src//a", True),
    ("/root", False), ("\\\\server\\share", False), ("C:\\a", False),
    ("a\\..\\b", False), ("", False), (".", False),
]
