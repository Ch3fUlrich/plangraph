"""Resolve caller-owned prompt input without copying it to disk."""
from pathlib import Path

from plangraph.planner.routing import SpawnRefused


class PromptInputRefused(SpawnRefused):
    """Preflight failures must not create run records."""
    preflight = True


def resolve_prompt(prompt: str | None, prompt_file: str | None) -> str:
    """Read UTF-8 (optionally BOM-prefixed), preserving the caller's text."""
    if prompt is not None and prompt_file is not None:
        raise SpawnRefused(f'prompt_input_conflict: {prompt_file}')
    if prompt is None and prompt_file is None:
        raise SpawnRefused('prompt_input_missing: give prompt or --prompt-file FILE')
    if prompt_file is None:
        validate_inputs(prompt)
        return prompt
    path = Path(prompt_file)
    try:
        if not path.is_file():
            raise PromptInputRefused(f'prompt_file_not_regular: {prompt_file}')
        with path.open(encoding='utf-8-sig', newline='') as source:
            text = source.read()
    except SpawnRefused:
        raise
    except UnicodeError:
        raise PromptInputRefused(f'prompt_file_invalid_utf8: {prompt_file}') from None
    except (OSError, ValueError):
        raise PromptInputRefused(f'prompt_file_unreadable: {prompt_file}') from None
    if not text.strip():
        raise PromptInputRefused(f'prompt_file_empty: {prompt_file}')
    return text


def validate_inputs(prompt, card=None):
    """Reject absent instructions and explicitly declared required inputs."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptInputRefused('prompt must not be blank')
    paths = (card or {}).get('input_files', [])
    if not isinstance(paths, (list, tuple)):
        raise SpawnRefused('card.input_files must be a list of paths')
    for path in paths:
        if not isinstance(path, str) or not path or not Path(path).is_file():
            raise PromptInputRefused(f'input_file_not_regular: {path}')


def admit_spawn(arguments):
    """Normalize prompt files before routing, persistence, or process creation."""
    text = resolve_prompt(arguments.get('prompt'), arguments.get('prompt_file'))
    validate_inputs(text, arguments.get('card'))
    return text
