"""Resolve caller-owned spawn inputs without copying them to disk."""
from importlib import resources
from pathlib import Path
import json
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


# Role header and role skill of every spawned prompt (rule 3.57). The header words come
# from config/role_headers.json, which the Rust native writer embeds too.
_ROLE_KEY = {'implement': 'implement', 'fix': 'implement', 'review': 'review'}
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _table() -> dict:
    return json.loads(resources.files('plangraph').joinpath('config/role_headers.json').read_text('utf-8'))


def agent_key(harness: str | None, leg_id: str | None = None) -> str:
    """Codex legs run through the opencode harness; their id says which agent they are."""
    if leg_id and leg_id.startswith('cx-'):
        return 'codex'
    return harness or 'opencode'


INLINE_TOOL_MODE = 'inline-only'


def header(harness: str | None, role: str, leg_id: str | None = None, tool_mode: str | None = None) -> str | None:
    key = _ROLE_KEY.get(role)
    if key is None:
        return None
    if key == 'review' and tool_mode == INLINE_TOOL_MODE:
        key = 'review_inline'  # a toolless seat is never told to run tests or open files
    table = _table()
    name = agent_key(harness, leg_id)
    return table['roles'][key].format(agent=table['agents'].get(name, 'the ' + name + ' agent'))


def is_batch_brief(brief: str) -> bool:
    return '\n# Items\n' in brief and '\n# Run values' in brief


def skill_name(role: str, brief: str) -> str | None:
    key = _ROLE_KEY.get(role)
    if key == 'review':
        return 'review'
    if key == 'implement':
        return 'batch-writer' if is_batch_brief(brief) else 'implement'
    return None


def read_skill(name: str, cwd: str | None) -> str | None:
    """Skill text from origin/main of the run checkout or this repository; when neither has
    origin/main, the working-tree copy of the same file."""
    roots = [r for r in (cwd, str(_REPO_ROOT)) if r]
    for root in roots:
        try:
            proc = subprocess.Popen(['git', '-C', root, 'show', f'origin/main:skills/{name}/SKILL.md'],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=checkout_env())
            out, _ = proc.communicate(timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and out:
            return out.decode('utf-8')
    for root in roots:
        try:
            text = (Path(root) / 'skills' / name / 'SKILL.md').read_bytes().decode('utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return text
    return None


def compose(prompt: str, role: str, harness: str | None, cwd: str | None, leg_id: str | None = None,
            tool_mode: str | None = None) -> str:
    """header (naming the leg's tool mode), role skill byte-identical, then the brief; neither is added twice."""
    head = header(harness, role, leg_id, tool_mode)
    if head is None:
        return prompt
    body = prompt[len(head):].lstrip('\n') if prompt.startswith(head) else prompt
    name = skill_name(role, body)
    skill = read_skill(name, cwd) if name else None
    if skill and not body.startswith(skill):
        body = skill + '\n' + body
    return head + '\n\n' + body
