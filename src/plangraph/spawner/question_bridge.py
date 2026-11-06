"""Question exit plumbing; no prompt injection or automatic resume."""
import hashlib
import json
import os


def answer_prompt(prompt, answer):
    return prompt + '\n\nAnswer to your question: ' + (answer[:4000] + '[... truncated]' if len(answer) > 4000 else answer)


def question_exit(job, record):
    events = [*record.get('events', ()), *getattr(job, 'events', ())]
    if (getattr(job, 'role', record.get('role')) == 'review'
            or record.get('reason') in ('git_config_mutated', 'cancelled', 'kill_incomplete', 'model_refusal')
            or getattr(job, 'reason', None) in ('git_config_mutated', 'cancelled', 'kill_incomplete', 'model_refusal')
            or any(e.get('event') in ('git_config_mutated', 'cancelled', 'kill_incomplete', 'model_refusal') for e in events)
            or getattr(job, 'survivors', ())
            or getattr(job, 'status', None) not in ('completed', 'failed', 'needs_input')):
        return False
    if not hasattr(job, 'question'):
        from .claude_stream import final_text
        text = getattr(job, 'final_message', None)
        if text is None:
            final = final_text(job.stdout)
            text = final[0] if final else None
        lines = text.splitlines() if text is not None else job.stdout.splitlines()[-1:]
        job.question = None
        for line in reversed(lines):
            if not line.lstrip().startswith('PG_QUESTION:'):
                continue
            try:
                q = json.loads(line.lstrip()[12:].strip())
                if not (isinstance(q, dict) and isinstance(q.get('text'), str) and q['text'].strip()
                        and (q.get('default') is None or isinstance(q['default'], str))):
                    raise ValueError('invalid question text/default')
                d = q.get('deadline_s')
                if not (d is None or (type(d) is int and 0 <= d <= 2**64 - 1)) or not (
                        isinstance(q.get('risk_tags', []), list) and all(isinstance(t, str) for t in q.get('risk_tags', []))):
                    raise ValueError('invalid question deadline/tags')
                job.question = {k: q.get(k, [] if k == 'risk_tags' else None) for k in ('text', 'default', 'deadline_s', 'risk_tags')}
                break
            except ValueError:
                job.events.append({'event': 'question_packet_malformed'})
    if job.question is None:
        return False
    job.status, job.exit_code = 'needs_input', 0
    record.update(status='needs_input', exit_code=0, question=job.question, reason='needs_input')
    return True


def rounds_exhausted(service, card, role):
    if not card or role == 'review':
        return False
    from .item_key import annotate_item
    identity = {'card_hash': hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()}
    annotate_item(identity, card)
    field = 'item_key' if identity.get('item_key') else 'card_hash'
    try:
        limit = int(os.environ.get('PLANGRAPH_MAX_QUESTION_ROUNDS', '3'))
    except ValueError:
        limit = 3
    return sum(r.get('status') == 'needs_input' and r.get(field) == identity[field]
               for r in service.ps()['runs']) >= limit
