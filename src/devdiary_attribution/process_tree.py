from __future__ import annotations

import ctypes
import os
import signal
import time
from ctypes import wintypes
from dataclasses import dataclass
from subprocess import Popen


@dataclass
class ProcessTree:
    process: Popen
    pgid: int | None = None
    job_handle: int | None = None
    windows_suspended: bool = False

    @classmethod
    def attach(cls, process: Popen) -> ProcessTree:
        if os.name == "nt":
            return cls(
                process=process,
                job_handle=_create_windows_job(process),
                windows_suspended=True,
            )
        return cls(process=process, pgid=process.pid)

    def resume(self) -> None:
        if not self.windows_suspended:
            return
        if self.job_handle is None:
            raise OSError("cannot resume a Windows process outside its Job Object")
        _resume_windows_process(self.process)
        self.windows_suspended = False

    def stop(self, grace_seconds: float = 5.0) -> None:
        if self.job_handle is not None:
            _close_windows_handle(self.job_handle)
            self.job_handle = None
            return
        if self.pgid is None:
            return

        _signal_group(self.pgid, signal.SIGTERM)
        if _wait_for_group_exit(self.pgid, grace_seconds):
            return
        _signal_group(self.pgid, signal.SIGKILL)
        _wait_for_group_exit(self.pgid, 1.0)

    def close(self) -> None:
        if self.job_handle is not None:
            _close_windows_handle(self.job_handle)
            self.job_handle = None


def _signal_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass


def _wait_for_group_exit(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _windows_dll():
    return vars(ctypes)["WinDLL"]("kernel32", use_last_error=True)


def _get_last_error() -> int:
    return vars(ctypes)["get_last_error"]()


def _windows_error(error: int) -> OSError:
    return vars(ctypes)["WinError"](error)


def _create_windows_job(process: Popen) -> int:
    kernel32 = _windows_dll()
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    assign_process = kernel32.AssignProcessToJobObject
    assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign_process.restype = wintypes.BOOL

    job = create_job(None, None)
    if not job:
        raise _windows_error(_get_last_error())

    information = _JobObjectExtendedLimitInformation()
    information.basic_limit_information.limit_flags = 0x00002000
    if not set_information(
        job, 9, ctypes.byref(information), ctypes.sizeof(information)
    ):
        error = _get_last_error()
        _close_windows_handle(job)
        raise _windows_error(error)

    process_handle = getattr(process, "_handle", None)
    if process_handle is None or not assign_process(
        job, wintypes.HANDLE(process_handle)
    ):
        error = _get_last_error()
        _close_windows_handle(job)
        raise _windows_error(error)
    return int(job)


def _close_windows_handle(handle: int) -> None:
    kernel32 = _windows_dll()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        raise _windows_error(_get_last_error())


def _resume_windows_process(process: Popen) -> None:
    process_handle = getattr(process, "_handle", None)
    if process_handle is None:
        raise OSError("Windows process handle is unavailable")
    ntdll = vars(ctypes)["WinDLL"]("ntdll", use_last_error=True)
    resume_process = ntdll.NtResumeProcess
    resume_process.argtypes = [wintypes.HANDLE]
    resume_process.restype = ctypes.c_long
    status = resume_process(wintypes.HANDLE(process_handle))
    if status < 0:
        raise OSError(f"NtResumeProcess failed with status 0x{status & 0xFFFFFFFF:08x}")


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_int64),
        ("per_job_user_time_limit", ctypes.c_int64),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobObjectBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]
