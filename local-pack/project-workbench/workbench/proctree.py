"""Owned process trees for native trusted command runs.

Windows: the process is created suspended, assigned to a Job Object with KILL_ON_JOB_CLOSE and
then resumed, so every descendant (Rscript.exe -> x64\\Rscript.exe -> Rterm.exe, plus anything R
starts) belongs to the job. Termination is confirmed by the job's active process count.
POSIX: a new session/process group; termination is confirmed when the group no longer exists.
This is lifecycle ownership only; it is not OS containment.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectBasicAccountingInformation = 1
    _JobObjectExtendedLimitInformation = 9
    _CREATE_SUSPENDED = 0x4

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _BasicAccounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    _ntdll.NtResumeProcess.restype = ctypes.c_long


class ProcessTree:
    """Start one command whose whole descendant tree can be stopped and verified."""

    def __init__(self):
        self.process = None
        self._job = None
        if os.name == "nt":
            job = _kernel32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                                     ctypes.byref(info), ctypes.sizeof(info)):
                error = ctypes.get_last_error()
                _kernel32.CloseHandle(job)
                raise OSError(error, "SetInformationJobObject failed")
            self._job = job

    def start(self, argv, **kwargs):
        if os.name != "nt":
            self.process = subprocess.Popen(argv, start_new_session=True, **kwargs)
            return self.process
        flags = kwargs.pop("creationflags", 0) | _CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(argv, creationflags=flags, **kwargs)
        handle = int(process._handle)
        if not _kernel32.AssignProcessToJobObject(self._job, handle) or _ntdll.NtResumeProcess(handle) != 0:
            error = ctypes.get_last_error()
            _kernel32.TerminateProcess(handle, 1)
            process.wait(timeout=10)
            raise OSError(error, "Could not place the command in its owned job")
        self.process = process
        return process

    def active_processes(self):
        """Processes still alive in the owned tree (Windows: job count; POSIX: 0/1 for the group)."""
        if os.name == "nt":
            info = _BasicAccounting()
            if not _kernel32.QueryInformationJobObject(self._job, _JobObjectBasicAccountingInformation,
                                                       ctypes.byref(info), ctypes.sizeof(info), None):
                raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
            return int(info.ActiveProcesses)
        if self.process is None:
            return 0
        if self.process.poll() is None:
            return 1
        try:
            os.killpg(self.process.pid, 0)
            return 1
        except ProcessLookupError:
            return 0
        except PermissionError:
            return 1

    def stop(self, timeout=10):
        """Kill every remaining process in the tree; return True only when none remain."""
        if os.name == "nt":
            if self.active_processes():
                _kernel32.TerminateJobObject(self._job, 1)
        elif self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.process is not None and self.process.poll() is None:
                time.sleep(.05)
                continue
            if self.active_processes() == 0:
                return True
            time.sleep(.05)
        return self.active_processes() == 0

    def close(self):
        if self._job:
            _kernel32.CloseHandle(self._job)  # KILL_ON_JOB_CLOSE stops any survivor.
            self._job = None
