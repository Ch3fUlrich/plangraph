"""Windows Job Object scope for spawner runs (split from runner.py).

``runner.WinJobApi`` re-exports :class:`WinJobApi`; tests patch it there.
"""
import ctypes


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [(n, t) for n, t in (
        ('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', ctypes.c_uint32), ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t), ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32))]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _JobBasicLimitInformation), ('IoInfo', ctypes.c_uint64 * 6),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class WinJobApi:
    """Kill-on-close Windows Job Object scope per run (SPAWN-ORPHAN-REAP F3).

    Created lazily on nt only; tests inject a fake into ``SpawnerRunner.win_job_api``
    so no ctypes loader runs off Windows. KILL_ON_JOB_CLOSE kills every descendant
    when the handle closes — what taskkill /T cannot do once the root pid exited."""

    def __init__(self):
        k32 = self._k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateJobObjectW.restype = k32.OpenProcess.restype = ctypes.c_void_p

    def create_job(self):
        k32, v = self._k32, ctypes.c_void_p
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), 'CreateJobObjectW')
        info = _JobExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(v(handle), 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(v(handle))
            raise OSError(ctypes.get_last_error(), 'SetInformationJobObject')
        return handle

    def assign(self, handle, pid):
        k32, v = self._k32, ctypes.c_void_p
        proc = k32.OpenProcess(0x101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not proc:
            raise OSError(ctypes.get_last_error(), 'OpenProcess')
        try:
            if not k32.AssignProcessToJobObject(v(handle), v(proc)):
                raise OSError(ctypes.get_last_error(), 'AssignProcessToJobObject')
        finally:
            k32.CloseHandle(v(proc))

    def terminate(self, handle):
        if not self._k32.TerminateJobObject(ctypes.c_void_p(handle), 1):
            raise OSError('TerminateJobObject failed')

    def cpu_seconds(self, handle):
        class Accounting(ctypes.Structure):
            _fields_ = [('times', ctypes.c_int64 * 4), ('counts', ctypes.c_uint32 * 4)]
        info = Accounting()
        if not self._k32.QueryInformationJobObject(ctypes.c_void_p(handle), 1,
                ctypes.byref(info), ctypes.sizeof(info), None):
            raise OSError('QueryInformationJobObject failed')
        return (info.times[0] + info.times[1]) / 1e7

    def close(self, handle):
        self._k32.CloseHandle(ctypes.c_void_p(handle))
