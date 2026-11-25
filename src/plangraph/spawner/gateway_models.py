"""Pure model identity admission and response-side call-log row parsing."""
from __future__ import annotations

from plangraph.routing_support.catalog_seed import local_provider_id


def model_segment(model):
    """Provider-independent identity; cx- is the route table's GPT alias prefix."""
    segment = str(model or '').rsplit('/', 1)[-1]
    return segment.removeprefix('cx-') if segment.startswith('cx-gpt-') else segment


def served_family(model):
    """Admit exact route-table model segments, never substrings in provider names."""
    from plangraph.planner.legs import DEFAULT_LEGS, EXPLICIT_LEGS
    families = {leg.family for leg in (*DEFAULT_LEGS, *EXPLICIT_LEGS)
                if model_segment(leg.model) == model_segment(model)}
    if len(families) == 1:
        family = families.pop()
        return 'gpt' if family == 'openai' else family
    return None


def served_models(rows, tag):
    """Every model the gateway served (2xx rows) for ``tag``, as ``provider/model``."""
    served = set()
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get('sessionTag') != tag:
            continue
        try:
            ok = 200 <= int(row.get('status')) < 300
        except (TypeError, ValueError):
            ok = False
        model = str(row.get('model') or '').strip()
        if ok and model:
            # Rows carry the gateway slug; the catalog row maps it back to the local prefix.
            # A slug two catalog rows claim names no provider: the row attests nothing.
            provider = str(row.get('provider') or '').strip()
            head, sep, rest = model.partition('/')
            provider = local_provider_id(provider) if provider else provider
            head = local_provider_id(head) if sep else head
            if provider is None or head is None:
                continue
            if sep:
                model = head + '/' + rest
            served.add(model if '/' in model or not provider else f'{provider}/{model}')
    return served


def record_served_witness(runner, job_id, model, *, adapter_token, finish_reason=None):
    """Record transport telemetry authenticated by an in-process adapter.

    Do not expose this capability to the child: even a child that knows its
    run nonce cannot attest to its own serving identity. Only a real
    admitted model id is attestable: a combo alias such as "l2-worker"
    (no "/") or any id outside the canonical families would otherwise
    publish the label itself as served_family. A refused witness leaves
    the job unwitnessed.
    """
    if adapter_token is None or adapter_token is not runner._adapter_tokens.get(job_id):
        raise ValueError('invalid adapter token')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('missing served model')
    model = model.strip()
    from .resolver import MODEL_FAMILIES
    family = served_family(model)
    if '/' not in model or family not in MODEL_FAMILIES:
        raise ValueError('served model is not an admitted model id: ' + model)
    job = runner.jobs[job_id]
    job.served_model = model
    job.served_family = family
    job.served_witness_reason = 'authenticated gateway adapter telemetry'
    job.response_finish_reason = finish_reason  # final witness telemetry, never inherited
