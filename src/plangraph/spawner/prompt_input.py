"""Resolve caller-owned prompt input without copying it to disk."""
from pathlib import Path

from plangraph.planner.routing import SpawnRefused


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
