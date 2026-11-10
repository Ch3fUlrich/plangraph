"""Platform options for background subprocesses, without shell or console UI."""
import os


def background_process_options(*, new_session=False, platform=None):
    """Keep Windows children out of Terminal while preserving process isolation.

    A process group alone does not suppress console creation. In a piped MCP
    or detached supervisor this can delegate native helpers to Windows Terminal
    with unusable handles. CREATE_NO_WINDOW keeps those helpers headless.
    """
    if (platform or os.name) == 'nt':
        return {'creationflags': 0x08000000 | (0x00000200 if new_session else 0)}
    return {'start_new_session': True} if new_session else {}
