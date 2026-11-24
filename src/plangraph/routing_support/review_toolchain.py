"""PATH of a review seat that runs the touched tests: the repository's pytest and cargo."""
import os
import shutil
import sys
from pathlib import Path


def pytest_bin_dir():
    """The bin directory holding pytest: the service's own venv, else the checkout's .venv."""
    for bin_dir in (Path(sys.prefix) / "bin", Path(__file__).resolve().parents[3] / ".venv/bin"):
        if (bin_dir / "pytest").is_file():
            return bin_dir
    return None


def review_tool_path(path, home):
    """``path`` with a pytest venv first (so `python -m pytest` works) and ~/.cargo/bin last.

    ``home`` is the seat's launching user's home, read before a seat swaps HOME for its own dir.
    """
    path = path or os.defpath
    venv = pytest_bin_dir()
    if venv is not None and str(venv) not in path.split(os.pathsep):
        path = str(venv) + os.pathsep + path
    cargo_bin = Path(home or Path.home()) / ".cargo/bin"
    if not shutil.which("cargo", path=path) and (cargo_bin / "cargo").is_file():
        path = path + os.pathsep + str(cargo_bin)
    return path
