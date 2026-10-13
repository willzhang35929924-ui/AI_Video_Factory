"""Check a process without sending a terminating signal on Windows."""
import ctypes
import errno
import os
from ctypes import wintypes


def probe_process(pid):
    if os.name != "nt":
        os.kill(pid, 0)
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            raise ProcessLookupError(errno.ESRCH, "Process not found", pid)
        if error == 5:
            raise PermissionError(errno.EACCES, "Process query denied", pid)
        raise ctypes.WinError(error)
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if exit_code.value != 259:
            raise ProcessLookupError(errno.ESRCH, "Process has exited", pid)
    finally:
        kernel.CloseHandle(handle)
