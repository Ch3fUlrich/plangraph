"""Resolve caller-owned spawn inputs without copying them to disk."""
from pathlib import Path
import subprocess

from plangraph.planner.routing import SpawnRefused
from plangraph.routing_errors import ParameterRefused


def resolve_cwd(cwd, card):
    """Validate explicit checkout paths; never reinterpret a relative path."""
    card_cwd = (card or {}).get('cwd')
    values = [cwd] if cwd is not None else []
    if card and 'cwd' in card:
        values.append(card_cwd)
    for value in values:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ParameterRefused('cwd must be an absolute path to an existing git checkout')
        if not Path(value).is_dir():
            raise ParameterRefused('cwd must name an existing directory')
        try:
            inside = subprocess.check_output(['git', '-C', value, 'rev-parse', '--is-inside-work-tree'],
                text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
        except (OSError, subprocess.SubprocessError):
            inside = ''
        if inside != 'true':
            raise ParameterRefused('cwd must name a git checkout (not a bare repository)')
    if cwd is not None and card_cwd is not None and Path(cwd).resolve() != Path(card_cwd).resolve():
        raise ParameterRefused('--cwd and card cwd disagree')
    return cwd if cwd is not None else card_cwd


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
