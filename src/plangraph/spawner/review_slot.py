"""Review seats' private build slots: allocation, lease and removal.

A slot is ``slot-review-<id>`` in the review pool (see host_ready_source.review_pool), which
admission cleanup and ``pg-disk-hygiene`` enumerate and trim; a lease on it keeps both away."""
import shutil
from pathlib import Path

from .host_ready import _cargo_lease_for, shared_cargo_target
from .host_ready_source import review_pool


def review_cargo_slot(job_id, leases=None):
    """Private build directory of one review seat: a ``slot-review-<id>`` of the capped pool.

    It sits beside the shared target in the same ``<target>-pool`` that admission cleanup and
    ``pg-disk-hygiene`` already enumerate and trim to ``PLANGRAPH_CARGO_TARGET_CAP_GB``. With
    ``leases``, the shared lease hygiene checks for this slot is taken first and kept in
    ``leases[job_id]`` until the seat ends, so a trim never removes a running seat's build.
    """
    pool = review_pool(shared_cargo_target())
    safe = ''.join(c for c in str(job_id) if c.isalnum() or c in '-_') or 'seat'
    slot = pool / f'slot-review-{safe}'
    if leases is not None:
        leases[job_id] = _cargo_lease_for(slot)
    slot.mkdir(parents=True, exist_ok=True, mode=0o700)
    return slot


def drop_review_slot(slot):
    """Remove one review slot and its lock file, under the slot's exclusive lease as the Rust side does.

    A seat or hygiene that holds the slot keeps it, and its lock file, where it is."""
    if not slot:
        return
    try:
        lease = _cargo_lease_for(Path(slot), exclusive=True)
    except OSError:
        return
    try:
        shutil.rmtree(slot, ignore_errors=True)
        # The slot's name is unique to one run, so once it is gone nothing asks for the lock.
        Path(str(slot) + '.plangraph-lock').unlink(missing_ok=True)
    finally:
        lease.close()


def remove_run_dir(path):
    """Remove a run's private directory: a review slot through ``drop_review_slot``, anything else outright."""
    p = Path(path)
    if p.name.startswith('slot-review-') and p.parent.name.endswith('-pool'):
        drop_review_slot(str(p))
    else:
        shutil.rmtree(path, ignore_errors=True)
