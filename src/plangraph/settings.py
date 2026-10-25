"""File-backed review policy; no keeper state or third-party dependencies."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import tomllib
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


def review_method(project=None, phase=None) -> ReviewMethod:
    """Resolve global, project and phase tables from PLANGRAPH_SETTINGS.

    Relative paths are relative to the repository root (nearest .git ancestor).
    No file is read unless the environment variable is set.
    """
    filename = os.environ.get('PLANGRAPH_SETTINGS')
    if not filename:
        return ReviewMethod()
    root = next((p for p in (Path.cwd(), *Path.cwd().parents)
                 if (p / '.git').exists()), Path.cwd())
    path = root / filename
    try:
        text = path.read_text(encoding='utf-8')
        data = json.loads(text) if path.suffix == '.json' else tomllib.loads(text)
    except (OSError, ValueError) as exc:
        raise SettingsError(f'file: {exc}') from exc
    if not isinstance(data, dict):
        raise SettingsError('file: expected settings table')
    values = {}
    source = 'default'
    layers = [('global', data)]
    for section, key in (('projects', project), ('phases', phase)):
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise SettingsError(f'{section}: expected table')
        if key is not None:
            layers.append((f'{section}:{key}', table.get(key, {})))
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
            source = f'{path}:{label}'
        ReviewMethod(**values)
    return ReviewMethod(**values, source=source)


def enforce_review_scope(card, role, review_scope):
    """Admission guard shared by both spawner implementations."""
    if review_scope not in (None, 'batch', 'per-fixer'):
        raise SettingsError('review_scope: expected batch or per-fixer')
    if role != 'review':
        return
    card = card or {}
    key = card.get('key', '')
    method = review_method(card.get('project') or key.split(':', 1)[0] or None,
                           card.get('phase'))
    if method.mode == 'batch' and (review_scope or card.get('review_scope')) == 'per-fixer':
        from plangraph.routing_errors import SpawnRefused
        exc = SpawnRefused('review_method_batch_per_fixer')
        exc.reason = exc.code = 'review_method_batch_per_fixer'
        raise exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['show'])
    parser.add_argument('--project')
    parser.add_argument('--phase')
    args = parser.parse_args()
    print(json.dumps(asdict(review_method(args.project, args.phase)), sort_keys=True))


if __name__ == '__main__':
    main()
