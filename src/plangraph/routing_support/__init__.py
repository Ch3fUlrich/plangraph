"""Shared routing policy and host adapters, independent of spawn execution."""
import os


def background_process_options(*, new_session=False, platform=None):
    """Suppress Windows consoles while retaining detached process ownership.

    A new process group alone can open Windows Terminal for a background child
    whose pipes have already closed. CREATE_NO_WINDOW keeps these launches hidden.
    Literal flags keep this helper importable and testable on every platform.
    """
    if (platform or os.name) == 'nt':
        return {'creationflags': 0x08000000 | (0x00000200 if new_session else 0)}
    return {'start_new_session': True} if new_session else {}
