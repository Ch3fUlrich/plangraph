"""Read-only role admission and fail-closed claim reports."""
from dataclasses import dataclass
import json
import re

ROLE = 'fact-check'
READ_ONLY_TOOLS = ('read', 'glob', 'grep', 'list')
ORDER = ('opencode-go-qwen3.8-max', 'opencode-go-deepseek-v4-flash', 'opencode-go-glm-5.2')


def permission_map(denied_tools: tuple[str, ...]) -> dict[str, str]:
    return {**dict.fromkeys(('*',) + denied_tools, 'deny'),
            **dict.fromkeys(READ_ONLY_TOOLS, 'allow')}


def read_only_tools_proven(config_json, env) -> bool:
    from .runner import _DENIED_TOOLS, _opencode_tools_denied
    try:
        cfg = json.loads(config_json)
        if cfg.get('permission') != permission_map(_DENIED_TOOLS):
            return False
        cfg['permission'] = dict.fromkeys(('*',) + _DENIED_TOOLS, 'deny')
        return _opencode_tools_denied(json.dumps(cfg), env)
    except (ValueError, TypeError, AttributeError):
        return False


def refuse_hand_pick(role, chosen, target):
    if role == ROLE and (chosen is None or chosen.trains_on_data
                         or target.endswith('-contributor')):
        from plangraph.routing_errors import SpawnRefused
        raise SpawnRefused('training_leg' if chosen else 'unknown_leg')


@dataclass(frozen=True)
class Claim:
    id: str
    state: str
    claim: str
    location: str
    evidence: str


@dataclass(frozen=True)
class FactReport:
    ok: bool
    reason: str | None
    claims: tuple[Claim, ...]


def parse_report(text: str) -> FactReport:
    def fail(reason):
        return FactReport(False, reason, ())
    if not text or not text.strip():
        return fail('empty_text')
    blocks = re.findall(r'^```fact-check\s*\n(.*?)^```[ \t]*$', text, re.M | re.S)
    if not blocks:
        return fail('no_block')
    if len(blocks) != 1:
        return fail('multiple_blocks')
    claims, seen = [], set()
    states = ('verified', 'false', 'unverifiable')
    for line in blocks[0].splitlines():
        fields = [field.strip() for field in line.split('|', 4)]
        if len(fields) < 5:
            return fail('field_count')
        if any(field.startswith('<') for field in fields):
            return fail('template_field')
        if not re.fullmatch(r'C\d+', fields[0]):
            return fail('invalid_id')
        if fields[0] in seen:
            return fail('duplicate_id')
        if fields[1] not in states:
            return fail('invalid_state')
        seen.add(fields[0])
        claims.append(Claim(*fields))
    last = re.fullmatch(r'FACTS: (\d+) verified, (\d+) false, (\d+) unverifiable', text.strip().splitlines()[-1])
    if not last:
        return fail('invalid_summary')
    if tuple(map(int, last.groups())) != tuple(sum(c.state == s for c in claims) for s in states):
        return fail('count_mismatch')
    if not claims:
        return fail('no_claims')
    return FactReport(True, None, tuple(claims))
