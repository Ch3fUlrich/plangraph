"""Resolve caller-owned spawn inputs without copying them to disk."""
from pathlib import Path
import os
import subprocess

from plangraph.planner.routing import SpawnRefused
from plangraph.routing_errors import ParameterRefused


def checkout_env():
    """Keep Git bound to the requested checkout, not a hook's repository."""
    return {k: v for k, v in os.environ.items() if k not in (
        'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
        'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_CONFIG', 'GIT_CONFIG_PARAMETERS')
        and not k.startswith(('GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))}


def resolve_cwd(cwd, card):
    """Validate explicit checkout paths; never reinterpret a relative path."""
    card_cwd = (card or {}).get('cwd')
    values = [cwd] if cwd is not None else []
    if card and 'cwd' in card:
        values.append(card_cwd)
    checked = set()
    resolved = []
    for value in values:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ParameterRefused('cwd must be an absolute path to an existing git checkout')
        if not Path(value).is_dir():
            raise ParameterRefused('cwd must name an existing directory')
        value = str(Path(value).resolve())
        resolved.append(value)
        if value in checked:
            continue
        try:
            inside, root, head = subprocess.check_output(['git', '-C', value, 'rev-parse',
                '--is-inside-work-tree', '--show-toplevel', '--verify', 'HEAD^{commit}'],
                text=True, encoding='utf-8', errors='replace', stderr=subprocess.DEVNULL,
                timeout=10, env=checkout_env()).strip().splitlines()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ParameterRefused('cwd must name a git checkout with a committed HEAD') from None
        if inside != 'true' or Path(root).resolve() != Path(value).resolve() or not head:
            raise ParameterRefused('cwd must name a git checkout root with a committed HEAD')
        checked.add(value)
    if len(set(resolved)) > 1:
        raise ParameterRefused('--cwd and card cwd disagree')
    return resolved[0] if resolved else None


def resolve_prompt(prompt: str | None, prompt_file: str | None) -> str:
    """Read UTF-8 (optionally BOM-prefixed), preserving the caller's text."""
    if prompt is not None and prompt_file is not None:
        raise SpawnRefused(f'prompt_input_conflict: {prompt_file}')
    if prompt is None and prompt_file is None:
        raise SpawnRefused('prompt_input_missing: give prompt or --prompt-file FILE')
    if prompt_file is None:
        return prompt
    path = Path(prompt_file)
    try:
        if not path.is_file():
            raise SpawnRefused(f'prompt_file_not_regular: {prompt_file}')
        with path.open(encoding='utf-8-sig', newline='') as source:
            text = source.read()
    except SpawnRefused:
        raise
    except UnicodeError:
        raise SpawnRefused(f'prompt_file_invalid_utf8: {prompt_file}') from None
    except (OSError, ValueError):
        raise SpawnRefused(f'prompt_file_unreadable: {prompt_file}') from None
    if not text.strip():
        raise SpawnRefused(f'prompt_file_empty: {prompt_file}')
    return text
