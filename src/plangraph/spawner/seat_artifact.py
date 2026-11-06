"""Fail-closed publication of final response-side review evidence."""
import json
import re
from pathlib import Path

from plangraph.review.verdict import judge
from .private_io import atomic_write_private


def publish(directory, record, job):
    """Publish evidence last; never recover evidence from combined run tails."""
    if record.get('role') != 'review' or record.get('ended') is None:
        return
    if job is None or record.get('job_id') not in (None, job.job_id):
        return  # retries without the owning final job cannot invalidate evidence
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('symlink run directory')
    review, evidence = directory / 'review.txt', directory / 'evidence.json'
    def clear():
        evidence.unlink(missing_ok=True)
        review.unlink(missing_ok=True)
    clear()  # unlink entries, never open a stale pair (including symlinks)
    if (job is None or record.get('status') != 'completed' or record.get('exit_code') != 0
            or record.get('job_id') not in (None, job.job_id) or job.status != 'completed'
            or job.exit_code != 0):
        return
    model = getattr(job, 'served_model', None)
    digest = getattr(job, 'prompt_sha256', None)
    finish = getattr(job, 'response_finish_reason', None)
    if (not model or model == 'unwitnessed' or finish not in ('stop', 'end_turn')
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        return
    verdict = judge(job.stdout, finish)
    if verdict.verdict is None or verdict.test_gaming == 'missing':
        return
    if verdict.verdict == 'pass' and verdict.test_gaming != 'no':
        return
    doc = dict(served_model=model, prompt_sha256=digest,
               correlation_id=job.job_id, run_id=record['run_id'],
               witness_kind='response-side', **verdict.to_evidence())
    doc.update(finish_reason=finish)
    try:
        atomic_write_private(review, job.stdout)
        atomic_write_private(evidence, json.dumps(doc, indent=2))
    except BaseException:
        clear()
        raise
