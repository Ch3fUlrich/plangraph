"""File-backed review policy; no keeper state or third-party dependencies."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
try:
    import tomllib
except ImportError:  # JSON policies remain usable on older Python.
    tomllib = None
from typing import Literal

_LABEL_SANITIZE_RE = re.compile(r'[/\\\x00-\x1f\x7f\x80-\x9f\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]')
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f\x80-\x9f\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]')


def _sanitize_label(key: str) -> str:
    return _LABEL_SANITIZE_RE.sub('_', key)


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
        if _CONTROL_CHARS_RE.search(self.source):
            raise SettingsError('source: control characters not allowed')


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


def _item_layers(item):
    """Return (project, phase) from a trusted keeper item; never from a card.

    ``item`` is a keeper item dict ('project', 'key' plus optional
    'phase'/'track') read from the store by keeper or planner code. The card's
    'project'/'phase'/'key' are caller-chosen and must not select a trusted
    policy layer (review-method R2, fix D-2773). A non-dict item yields no
    item layers instead of crashing (D-2784); the item key falls back as the
    phase, as cards did before.
    """
    if not isinstance(item, dict):
        return None, None
    phase = item.get('phase') or item.get('track') or item.get('key')
    project = item.get('project') or project_of(item.get('key') or phase)
    return project, phase


def _card_layers(card):
    """Return (project, phase) selected by the card for R3 tightening layers.

    The card's 'project'/'phase' (or its key's project) select the layers that
    may only tighten (D-2784). The settings root is never taken from the card
    (R1); this only names which layers of the trusted policy file apply.
    """
    if not isinstance(card, dict):
        return None, None
    return card.get('project') or project_of(card.get('key')), card.get('phase')


def _policy_data(root):
    """Load the policy table for ``root``; None when PLANGRAPH_SETTINGS is unset.

    Relative paths are relative to the repository root (nearest .git ancestor).
    No file is read unless the environment variable is set.
    """
    filename = os.environ.get('PLANGRAPH_SETTINGS')
    if not filename:
        return None
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
    return data


def _merge_layer_values(layers):
    """Merge review_method overrides across (label, table) layers.

    Later layers override earlier ones ('seats' merges instead) and every
    intermediate combination is validated. Returns (values, source).
    """
    values = {}
    source = 'default'
    for label, table in layers:
        if not isinstance(table, dict):
            raise SettingsError('expected table')
        override = table.get('review_method', {})
        if not isinstance(override, dict):
            raise SettingsError('review_method: expected table')
        for name, value in override.items():
            if name not in ReviewMethod.__dataclass_fields__ or name == 'source':
                raise SettingsError(f'{_sanitize_label(str(name))}: unknown review_method setting')
            values[name] = ({**values.get(name, ReviewMethod().seats), **value}
                            if name == 'seats' and isinstance(value, dict) else value)
        if override:
            source = label
        ReviewMethod(**values)
    return values, source


def review_method(project=None, phase=None, *, root=None) -> ReviewMethod:
    """Resolve global, project and phase tables from PLANGRAPH_SETTINGS.

    The trusted layers (global plus any project/phase layer) may loosen or
    tighten (R2). Root resolution is an explicit ``root=`` argument, else
    PLANGRAPH_REPO_ROOT / cwd resolution applies (R1).
    """
    data = _policy_data(root)
    if data is None:
        return ReviewMethod()
    if project is None and phase:
        project = project_of(phase)
    layers = [('global', data)]
    for section, key in (('projects', project), ('phases', phase)):
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise SettingsError('expected table')
        if key is not None:
            safe_key = _sanitize_label(key)
            layers.append((f'{section}:{safe_key}', table.get(key, {})))
    values, source = _merge_layer_values(layers)
    return ReviewMethod(**values, source=source)


def _card_layer_values(card, root):
    """Overrides set by the layers the card's project/phase select (R3).

    Only those layers are merged -- never the global layer -- so the result is
    exactly what the card-selected layers contribute (D-2784). Any resolution
    error propagates; callers fail closed (scope) or fall back to the trusted
    result (threshold).
    """
    project, phase = _card_layers(card)
    if project is None and phase is None:
        return {}
    data = _policy_data(root)
    if data is None:
        return {}
    layers = []
    for section, key in (('projects', project), ('phases', phase)):
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise SettingsError('expected table')
        if key is not None:
            layers.append((f'{section}:{_sanitize_label(key)}', table.get(key, {})))
    values, _source = _merge_layer_values(layers)
    return values


def _card_layer_batch(card, root):
    """True when the card-selected layers tighten the mode to 'batch' (R3).

    A resolution error fails closed to True: a per-fixer seat is the risky
    direction, so a broken tightening layer must not admit one (D-2784).
    """
    try:
        return _card_layer_values(card, root).get('mode') == 'batch'
    except Exception:
        return True


def fixer_threshold(*, item=None, root=None, card=None) -> int:
    """Resolve fixer split threshold for a trusted keeper item context.

    Card fields never select the trusted layers or the settings root
    (review-method R1/R2); the layers the card's project/phase select may only
    tighten (R3, D-2784): the threshold is min(trusted, card-selected) and an
    error resolving the card layers falls back to the trusted value alone.
    """
    project, phase = _item_layers(item)
    trusted = review_method(project, phase, root=root).fixer_split_threshold
    if card is None:
        return trusted
    try:
        card_value = _card_layer_values(card, root).get('fixer_split_threshold')
    except Exception:
        return trusted
    return min(trusted, card_value) if card_value is not None else trusted


def enforce_review_scope(card, role, review_scope, *, root=None, item=None):
    """Admission guard shared by both spawner implementations.

    The settings root is never taken from the card (R1): ``root`` is passed by
    server-side code only, else PLANGRAPH_REPO_ROOT / cwd resolution applies.
    Trusted layers come from the keeper ``item`` and may loosen or tighten (R2);
    the layers the card's project/phase select may only tighten (R3, D-2784):
    a per-fixer scope is refused when either resolves to ``batch``, and an
    error resolving the card layers fails closed to ``batch``.
    """
    card = card or {}
    if any(value not in (None, 'batch', 'per-fixer')
           for value in (review_scope, card.get('review_scope'))):
        raise SettingsError('review_scope: expected batch or per-fixer')
    if role != 'review' and (review_scope is not None or card.get('review_scope') is not None):
        raise SettingsError('review_scope only valid for role=review')
    project, phase = _item_layers(item)
    method = review_method(project, phase, root=root)
    if (review_scope == 'per-fixer' or
            (review_scope is None and card.get('review_scope') == 'per-fixer')):
        if method.mode == 'batch' or _card_layer_batch(card, root):
            from plangraph.routing_errors import SpawnRefused
            exc = SpawnRefused('review_method_batch_per_fixer')
            exc.reason = exc.code = 'review_method_batch_per_fixer'
            raise exc


def as_spawn_refused(exc: SettingsError):
    """Convert SettingsError to SpawnRefused('review_method_invalid')."""
    from plangraph.routing_errors import SpawnRefused
    detail = str(exc)
    msg = f'review_method_invalid: {detail}' if detail else 'review_method_invalid'
    err = SpawnRefused(msg)
    err.reason = err.code = 'review_method_invalid'
    return err


def admit_review_scope(card, role, review_scope, *, root=None, item=None):
    """Validate review scope for admission, converting SettingsError to SpawnRefused."""
    try:
        return enforce_review_scope(card, role, review_scope, root=root, item=item)
    except SettingsError as exc:
        raise as_spawn_refused(exc) from None


def admit_fixer_threshold(*, item=None, root=None, card=None) -> int:
    """Resolve fixer split threshold for admission, converting SettingsError to SpawnRefused."""
    try:
        return fixer_threshold(item=item, root=root, card=card)
    except SettingsError as exc:
        raise as_spawn_refused(exc) from None



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
