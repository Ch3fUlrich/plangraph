"""File-backed review policy; no keeper state or third-party dependencies."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
try:
    import tomllib
except ImportError:  # JSON policies remain usable on older Python.
    tomllib = None
from typing import Literal


class SettingsError(ValueError):
    """Invalid named setting or settings file."""


@dataclass
class ReviewMethod:
    mode: Literal['batch', 'per-item'] = 'batch'
    seats: dict[str, int] = field(default_factory=lambda: {'standard': 1, 'high': 2})
    distinct_families: bool = True
    family_source: str = 'D-1704'
    max_rounds: int = 3
    fixer_split_threshold: int = 10
    source: str = 'default'

    def __post_init__(self):
        if isinstance(self.seats, dict) and set(self.seats) - {'standard', 'high'}:
            raise SettingsError('seats: unsupported tier')
        if self.mode not in ('batch', 'per-item'):
            raise SettingsError('mode: expected batch or per-item')
        if (not isinstance(self.seats, dict) or set(self.seats) != {'standard', 'high'}
                or any(type(v) is not int or v < 1 for v in self.seats.values())):
            raise SettingsError('seats: expected positive standard and high integers')
        if type(self.distinct_families) is not bool:
            raise SettingsError('distinct_families: expected boolean')
        if not isinstance(self.family_source, str) or not self.family_source.strip():
            raise SettingsError('family_source: expected nonempty string')
        for name in ('max_rounds', 'fixer_split_threshold'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise SettingsError(f'{name}: expected positive integer')
        if not isinstance(self.source, str) or not self.source.strip():
            raise SettingsError('source: expected nonempty string')
        if '/' in self.source or '\\' in self.source:
            raise SettingsError('source: path separators not allowed')


def project_of(card_or_key) -> str | None:
    """Derive project name from a card dictionary or string key/phase.

    A colon-free key derives the whole key as project. Returns None if
    card_or_key is empty or missing.
    """
    if isinstance(card_or_key, dict):
        project = card_or_key.get('project')
        if project:
            return project
        raw = card_or_key.get('key') or card_or_key.get('phase')
        if not raw:
            return None
        return raw.split(':', 1)[0]
    if isinstance(card_or_key, str) and card_or_key:
        return card_or_key.split(':', 1)[0]
    return None


def review_method(project=None, phase=None, *, root=None) -> ReviewMethod:
    """Resolve global, project and phase tables from PLANGRAPH_SETTINGS.

    Relative paths are relative to the repository root (nearest .git ancestor).
    No file is read unless the environment variable is set.
    """
    filename = os.environ.get('PLANGRAPH_SETTINGS')
    if not filename:
        return ReviewMethod()
    base = Path(root or os.environ.get('PLANGRAPH_REPO_ROOT') or Path.cwd())
    root = next((p for p in (base, *base.parents)
                 if (p / '.git').exists()), base)
    path = root / filename
    suffix = path.suffix.lower()
    if suffix != '.json' and tomllib is None:
        raise SettingsError('file: tomllib unavailable; use JSON settings')
    try:
        text = path.read_text(encoding='utf-8')
        data = json.loads(text) if suffix == '.json' else tomllib.loads(text)
    except (OSError, ValueError) as exc:
        location = f' line {exc.lineno} column {exc.colno}' if hasattr(exc, 'lineno') else ''
        raise SettingsError(f'file: {type(exc).__name__}{location}') from None
    if not isinstance(data, dict):
        raise SettingsError('file: expected settings table')
    if project is None and phase:
        project = project_of(phase)
    values = {}
    source = 'default'
    layers = [('global', data)]
    for section, key in (('projects', project), ('phases', phase)):
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise SettingsError(f'{section}: expected table')
        if key is not None:
            safe_key = key.replace('/', '_').replace('\\', '_')
            layers.append((f'{section}:{safe_key}', table.get(key, {})))
    for label, table in layers:
        if not isinstance(table, dict):
            raise SettingsError(f'{label}: expected table')
        override = table.get('review_method', {})
        if not isinstance(override, dict):
            raise SettingsError(f'{label}.review_method: expected table')
        for name, value in override.items():
            if name not in ReviewMethod.__dataclass_fields__ or name == 'source':
                raise SettingsError(f'{name}: unknown review_method setting')
            values[name] = ({**values.get(name, ReviewMethod().seats), **value}
                            if name == 'seats' and isinstance(value, dict) else value)
        if override:
            source = label
        ReviewMethod(**values)
    return ReviewMethod(**values, source=source)


def fixer_threshold(card=None) -> int:
    """Resolve fixer split threshold for card context."""
    card = card or {}
    project = project_of(card)
    method = review_method(project, card.get('phase'), root=card.get('repo_root'))
    return method.fixer_split_threshold


def enforce_review_scope(card, role, review_scope, *, root=None):
    """Admission guard shared by both spawner implementations."""
    card = card or {}
    if any(value not in (None, 'batch', 'per-fixer')
           for value in (review_scope, card.get('review_scope'))):
        raise SettingsError('review_scope: expected batch or per-fixer')
    if role != 'review' and (review_scope is not None or card.get('review_scope') is not None):
        raise SettingsError('review_scope only valid for role=review')
    project = project_of(card)
    method = review_method(project,
                           card.get('phase'), root=root or card.get('repo_root'))
    if method.mode == 'batch' and (review_scope == 'per-fixer' or
            (review_scope is None and card.get('review_scope') == 'per-fixer')):
        from plangraph.routing_errors import SpawnRefused
        exc = SpawnRefused('review_method_batch_per_fixer')
        exc.reason = exc.code = 'review_method_batch_per_fixer'
        raise exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['show'])
    parser.add_argument('--project')
    parser.add_argument('--phase')
    parser.add_argument('--root')
    args = parser.parse_args()
    print(json.dumps(asdict(review_method(args.project, args.phase, root=args.root)), sort_keys=True))


if __name__ == '__main__':
    main()
