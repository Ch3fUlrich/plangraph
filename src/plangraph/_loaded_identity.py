"""Sample provenance before package imports; never discover an enclosing repo."""
import os
from pathlib import Path
import subprocess

CODE_TREE = Path(__file__).resolve().parents[2]


def repo_git(repo, *args, check=True):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env['GIT_TERMINAL_PROMPT'] = '0'
    return subprocess.run(['git', '-C', str(repo), *args], check=check, capture_output=True,
                          text=True, timeout=30, env=env)


def code_identity(repo):
    try:
        if Path(repo_git(repo, 'rev-parse', '--show-toplevel').stdout.strip()).resolve() != repo.resolve():
            return None, None, None
        sha = repo_git(repo, 'rev-parse', '--verify', 'HEAD^{commit}').stdout.strip()
        branch = repo_git(repo, 'symbolic-ref', '--short', '-q', 'HEAD', check=False).stdout.strip() or None
        flags = repo_git(repo, 'ls-files', '-v').stdout.splitlines()
        dirty = (bool(repo_git(repo, 'status', '--porcelain', '--untracked-files=no').stdout)
                 or any(line[0] != 'H' for line in flags))
        return None if dirty else sha, branch, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None, None


LOADED_IDENTITY = code_identity(CODE_TREE)
