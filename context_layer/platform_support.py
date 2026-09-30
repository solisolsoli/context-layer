"""Small, fail-closed cross-platform primitives for Context Layer runtime code.

Windows support uses only the standard library plus documented Win32 APIs via
ctypes. Private files get a protected DACL for their owner and SYSTEM; an ACL
failure closes and removes the staging file before any caller can write data.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import math
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import Iterator

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:  # pragma: no cover - exercised by Windows CI
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl


class LockTimeout(TimeoutError):
    """A portable file lock was not acquired before its deadline."""


class ProcessTreeError(OSError):
    """A child could not be safely placed in an isolated process tree."""


def is_link_or_reparse(path) -> bool:
    """Whether the leaf is a symlink or Windows reparse point, without following it.

    A missing leaf is false; inspection errors other than absence propagate so callers
    cannot mistake an inaccessible path for a safe ordinary file.
    """
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400))


@contextmanager
def file_lock(path, *, timeout: float | None = 30.0,
              poll_interval: float = 0.025) -> Iterator[None]:
    """Hold an exclusive cross-process lock until context exit.

    The lock file is persistent and must not be unlinked by callers: unlinking a
    locked inode can let another process lock a different inode at the same path.
    A finite timeout is the default on both platforms.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        valid_timeout = (not isinstance(timeout, bool) and isinstance(timeout, (int, float))
                        and math.isfinite(timeout) and timeout >= 0)
    except OverflowError:
        valid_timeout = False
    if not valid_timeout or timeout > 86400:
        raise ValueError("timeout must be finite and between 0 and 86400 seconds")
    try:
        valid_poll = (not isinstance(poll_interval, bool)
                      and isinstance(poll_interval, (int, float))
                      and math.isfinite(poll_interval) and poll_interval > 0)
    except OverflowError:
        valid_poll = False
    if not valid_poll or poll_interval > 3600:
        raise ValueError("poll_interval must be finite and between 0 and 3600 seconds")
    flags = (os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(target, flags, 0o600)
    try:
        opened = os.fstat(fd)
        on_path = os.stat(target, follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(on_path.st_mode)
                or not os.path.samestat(opened, on_path)
                or (IS_WINDOWS and getattr(on_path, "st_file_attributes", 0) & 0x400)):
            raise OSError(errno.EINVAL, "lock path is not a stable regular file", str(target))
        # The lock file is persistent and may contain the initialization byte.
        # Windows ignores POSIX mode bits, so secure the exact open file handle.
        set_private_permissions(fd, target)
    except BaseException:
        os.close(fd)
        raise
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        if IS_WINDOWS:
            # Lock byte zero first (Win32 permits ranges beyond EOF), then initialize
            # under that lock. This avoids two first users racing on an empty file.
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EDEADLK, errno.EAGAIN,
                                         errno.EPERM, errno.EIO):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"timed out waiting for lock: {target.name}") from None
                    time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
            if os.fstat(fd).st_size == 0:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\0")
                os.fsync(fd)
        else:
            operation = fcntl.LOCK_EX | fcntl.LOCK_NB
            while True:
                try:
                    fcntl.flock(fd, operation)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"timed out waiting for lock: {target.name}") from None
                    time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try:
            if acquired:
                if IS_WINDOWS:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _win_error(api: str) -> OSError:  # pragma: no cover - Windows only
    return ctypes.WinError(ctypes.get_last_error(), f"{api} failed")


def _set_windows_private_acl(path: Path, *, directory: bool = False, fd: int | None = None,
                            apply: bool = True) -> None:
    """Install and verify a protected DACL granting only current user and SYSTEM."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    LPCWSTR, DWORD, BOOL, LPVOID = wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.LPVOID
    descriptor = LPVOID()
    needed = DWORD()
    dacl = LPVOID()
    present = BOOL()
    defaulted = BOOL()
    control = wintypes.WORD()
    token = wintypes.HANDLE()
    user_sid_text = wintypes.LPWSTR()

    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        LPCWSTR, DWORD, ctypes.POINTER(LPVOID), ctypes.POINTER(DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = BOOL
    advapi.SetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, DWORD, LPVOID, LPVOID,
                                      LPVOID, LPVOID]
    advapi.SetSecurityInfo.restype = DWORD
    advapi.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, DWORD,
                                      ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID),
                                      ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID),
                                      ctypes.POINTER(LPVOID)]
    advapi.GetSecurityInfo.restype = DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [LPVOID, ctypes.POINTER(wintypes.WORD),
                                                     ctypes.POINTER(DWORD)]
    advapi.GetSecurityDescriptorControl.restype = BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [LPVOID, ctypes.POINTER(BOOL),
                                                 ctypes.POINTER(LPVOID), ctypes.POINTER(BOOL)]
    advapi.GetSecurityDescriptorDacl.restype = BOOL
    advapi.GetAclInformation.argtypes = [LPVOID, LPVOID, DWORD, ctypes.c_int]
    advapi.GetAclInformation.restype = BOOL
    advapi.GetAce.argtypes = [LPVOID, DWORD, ctypes.POINTER(LPVOID)]
    advapi.GetAce.restype = BOOL
    advapi.EqualSid.argtypes = [LPVOID, LPVOID]
    advapi.EqualSid.restype = BOOL
    advapi.ConvertStringSidToSidW.argtypes = [LPCWSTR, ctypes.POINTER(LPVOID)]
    advapi.ConvertStringSidToSidW.restype = BOOL
    advapi.ConvertSidToStringSidW.argtypes = [LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, LPVOID, DWORD,
                                           ctypes.POINTER(DWORD)]
    advapi.GetTokenInformation.restype = BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = BOOL
    kernel.CreateFileW.argtypes = [LPCWSTR, DWORD, DWORD, LPVOID, DWORD, DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [("dwFileAttributes", DWORD), ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME),
                    ("ftLastWriteTime", wintypes.FILETIME), ("dwVolumeSerialNumber", DWORD),
                    ("nFileSizeHigh", DWORD), ("nFileSizeLow", DWORD),
                    ("nNumberOfLinks", DWORD), ("nFileIndexHigh", DWORD),
                    ("nFileIndexLow", DWORD)]
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE,
                                                   ctypes.POINTER(_ByHandleFileInformation)]
    kernel.GetFileInformationByHandle.restype = BOOL
    kernel.ReOpenFile.argtypes = [wintypes.HANDLE, DWORD, DWORD, DWORD]
    kernel.ReOpenFile.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [LPVOID]
    kernel.LocalFree.restype = LPVOID

    class _AclSizeInfo(ctypes.Structure):
        _fields_ = [("AceCount", DWORD), ("AclBytesInUse", DWORD), ("AclBytesFree", DWORD)]

    class _AceHeader(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte),
                    ("AceSize", wintypes.WORD)]

    class _AllowedAce(ctypes.Structure):
        _fields_ = [("Header", _AceHeader), ("Mask", DWORD), ("SidStart", DWORD)]

    user_sid, system_sid = LPVOID(), LPVOID()
    user_sid_ptr = None
    owned_handle = False
    file_handle = None
    sd = LPVOID()
    try:
        if fd is not None:
            original = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
            access = 0x00020000 | (0x00040000 if apply else 0)
            file_handle = kernel.ReOpenFile(original, access, 7, 0)
            if (not file_handle
                    or _win_handle_value(file_handle) == ctypes.c_void_p(-1).value):
                file_handle = None
                raise _win_error("ReOpenFile(private ACL)")
            owned_handle = True
        else:
            # Open the leaf itself (including a reparse point), never its target.
            flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT
            access = 0x00020000 | (0x00040000 if apply else 0)
            file_handle = kernel.CreateFileW(str(path), access, 7, None,
                                             3, flags, None)  # READ_CONTROL | WRITE_DAC
            if (not file_handle
                    or _win_handle_value(file_handle) == ctypes.c_void_p(-1).value):
                file_handle = None
                raise _win_error("CreateFileW(private ACL)")
            owned_handle = True
        file_info = _ByHandleFileInformation()
        if not kernel.GetFileInformationByHandle(file_handle, ctypes.byref(file_info)):
            raise _win_error("GetFileInformationByHandle(private ACL)")
        if file_info.dwFileAttributes & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise OSError(errno.ELOOP, "private ACL path is a reparse point", str(path))
        is_directory = bool(file_info.dwFileAttributes & 0x00000010)  # FILE_ATTRIBUTE_DIRECTORY
        if is_directory != directory:
            raise OSError(errno.EINVAL, "private ACL path has the wrong file type", str(path))
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise _win_error("OpenProcessToken")
        token_bytes = DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(token_bytes))  # TokenUser
        if not token_bytes.value:
            raise _win_error("GetTokenInformation(size)")
        token_user_buffer = ctypes.create_string_buffer(token_bytes.value)
        if not advapi.GetTokenInformation(token, 1, token_user_buffer, token_bytes,
                                          ctypes.byref(token_bytes)):
            raise _win_error("GetTokenInformation")
        # TOKEN_USER starts with SID_AND_ATTRIBUTES; its first member is the SID pointer.
        user_sid_ptr = ctypes.cast(token_user_buffer, ctypes.POINTER(LPVOID)).contents
        if not user_sid_ptr:
            raise OSError("current user token has no SID")
        if not advapi.ConvertSidToStringSidW(user_sid_ptr, ctypes.byref(user_sid_text)):
            raise _win_error("ConvertSidToStringSidW")
        inheritance = "OICI" if directory else ""
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f"D:P(A;{inheritance};FA;;;SY)(A;{inheritance};FA;;;{user_sid_text.value})", 1,
                ctypes.byref(descriptor), ctypes.byref(needed)):
            raise _win_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise _win_error("GetSecurityDescriptorDacl(template)")
        if not present.value or not dacl:
            raise OSError("private ACL template has no DACL")
        if apply:
            result = advapi.SetSecurityInfo(file_handle, 1, 0x00000004 | 0x80000000,
                                            None, None, dacl, None)
            if result:
                raise ctypes.WinError(result, "SetSecurityInfo failed")
        advapi.ConvertStringSidToSidW(user_sid_text.value, ctypes.byref(user_sid))
        if not user_sid:
            raise _win_error("ConvertStringSidToSidW(current user)")
        advapi.ConvertStringSidToSidW("S-1-5-18", ctypes.byref(system_sid))
        if not system_sid:
            raise _win_error("ConvertStringSidToSidW(SYSTEM)")

        # Read back from this exact open handle, rejecting inherited/broad entries.
        owner, group, sacl = LPVOID(), LPVOID(), LPVOID()
        result = advapi.GetSecurityInfo(file_handle, 1, 0x00000004, ctypes.byref(owner),
                                        ctypes.byref(group), ctypes.byref(dacl),
                                        ctypes.byref(sacl), ctypes.byref(sd))
        if result:
            raise ctypes.WinError(result, "GetSecurityInfo failed")
        if not dacl or not sd:
            raise OSError("private ACL verification found no DACL")
        revision = DWORD()
        if not advapi.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
            raise _win_error("GetSecurityDescriptorControl")
        protected = bool(control.value & 0x1000)  # SE_DACL_PROTECTED
        if directory and not protected:
            raise OSError("private ACL verification found an inheritable DACL")
        info = _AclSizeInfo()
        if not advapi.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise _win_error("GetAclInformation")
        if info.AceCount != 2:
            raise OSError("private ACL verification found unexpected access entries")
        found = set()
        for index in range(info.AceCount):
            ace_ptr = LPVOID()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace_ptr)):
                raise _win_error("GetAce")
            ace = ctypes.cast(ace_ptr, ctypes.POINTER(_AllowedAce)).contents
            flags_ok = (ace.Header.AceFlags == 0x03 if directory else
                        ace.Header.AceFlags == 0 if protected else
                        ace.Header.AceFlags in (0, 0x10))
            if (ace.Header.AceType != 0 or not flags_ok
                    or ace.Mask != 0x1F01FF):
                raise OSError("private ACL verification found a non-owner access entry")
            sid_ptr = ctypes.cast(ctypes.addressof(ace) + _AllowedAce.SidStart.offset,
                                  LPVOID)
            if advapi.EqualSid(sid_ptr, user_sid):
                found.add("user")
            elif advapi.EqualSid(sid_ptr, system_sid):
                found.add("system")
            else:
                raise OSError("private ACL verification found an unexpected principal")
        if found != {"user", "system"}:
            raise OSError("private ACL verification missed current user or SYSTEM access")
    finally:
        for value in (descriptor, user_sid, system_sid, user_sid_text, sd):
            if value:
                kernel.LocalFree(value)
        if token:
            _win_close_handle(kernel, token, "CloseHandle(process token)")
        if owned_handle and file_handle:
            _win_close_handle(kernel, file_handle, "CloseHandle(private ACL)")


def set_private_permissions(fd: int, path) -> None:
    """Restrict an open file to its owner (and SYSTEM on Windows), failing closed."""
    if IS_WINDOWS:
        _set_windows_private_acl(Path(path), fd=fd)
    else:
        os.fchmod(fd, 0o600)


def set_private_path(path, *, directory: bool = False) -> None:
    """Restrict an existing file or directory to its owner, failing closed."""
    target = Path(path)
    if IS_WINDOWS:
        _set_windows_private_acl(target, directory=directory)
    else:
        os.chmod(target, 0o700 if directory else 0o600)


def verify_private_path(path, *, directory: bool = False) -> None:
    """Read-only check that a file or inherited child has only user+SYSTEM access."""
    target = Path(path)
    if IS_WINDOWS:
        _set_windows_private_acl(target, directory=directory, apply=False)
    else:
        info = target.stat()
        if (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode)):
            raise OSError("private path has the wrong file type")
        if stat.S_IMODE(info.st_mode) & (0o077 if directory else 0o077):
            raise OSError("private path grants group or other permissions")


def private_tempfile(directory, *, prefix: str = ".tmp-", suffix: str = "") -> tuple[int, Path]:
    """Create a private same-directory staging file, secured before caller writes."""
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    target = Path(name)
    try:
        set_private_permissions(fd, target)
        return fd, target
    except BaseException:
        try:
            os.close(fd)
        finally:
            try:
                target.unlink()
            except OSError:
                pass
        raise


def private_tempdir(directory=None, *, prefix: str = "context-layer-") -> Path:
    """Create a private directory with an inheritable user+SYSTEM DACL on Windows."""
    target = Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
    try:
        set_private_path(target, directory=True)
        return target
    except BaseException:
        try:
            target.rmdir()
        except OSError:
            pass
        raise


def _create_staging(path: Path, mode: int, private: bool) -> tuple[int, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        return private_tempfile(path.parent, prefix=f".{path.name}.", suffix=".tmp")
    for _ in range(10):
        staging = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_BINARY", 0), mode)
            return fd, staging
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate an atomic staging file")


def atomic_write(path, data: bytes | str, *, mode: int = 0o666,
                 private: bool = False) -> None:
    """Atomically replace a file with bytes (text encodes to UTF-8 without newline rewriting)."""
    target = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    fd, staging = _create_staging(target, mode, private)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows can briefly retain a rename/delete handle while another
        # writer replaces the same name. Retry only those sharing/access errors;
        # persistent permission failures still fail and retain the old file.
        deadline = time.monotonic() + 1.0
        delay = 0.002
        while True:
            try:
                os.replace(staging, target)
                break
            except PermissionError as exc:
                if not IS_WINDOWS or getattr(exc, "winerror", None) not in (5, 32, 33):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.05)
    except BaseException:
        try:
            staging.unlink()
        except OSError:
            pass
        raise


def process_is_alive(pid: int) -> bool:
    """Conservative PID liveness check without sending a signal on Windows."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            return True
    kernel = _win_kernel()
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER means no such PID
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        _win_close_handle(kernel, handle, "CloseHandle(process query)")


# Win32 Job Object constants and ABI definitions are declared only on Windows.
if IS_WINDOWS:  # pragma: no cover - exercised by Windows CI
    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _AccountingInfo(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_longlong),
                    ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                    ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", wintypes.DWORD),
                    ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD),
                    ("TotalTerminatedProcesses", wintypes.DWORD)]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]


def _win_kernel():  # pragma: no cover - Windows only
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel.Thread32First.restype = wintypes.BOOL
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel.Thread32Next.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    return kernel


def _win_handle_value(handle) -> int:  # pragma: no cover - Windows only
    return int(handle.value if hasattr(handle, "value") else handle)


def _win_close_handle(kernel, handle, api: str = "CloseHandle") -> None:  # pragma: no cover
    if handle and not kernel.CloseHandle(handle):
        raise _win_error(api)


def _win_create_job(kernel):  # pragma: no cover - Windows only
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise _win_error("CreateJobObjectW")
    limits = _ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = _win_error("SetInformationJobObject")
        _win_close_handle(kernel, job)
        raise error
    return job


def _win_resume_primary_thread(kernel, pid: int) -> None:  # pragma: no cover - Windows only
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or _win_handle_value(snapshot) == invalid:
        raise _win_error("CreateToolhelp32Snapshot")
    thread = None
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        found = []
        ok = kernel.Thread32First(snapshot, ctypes.byref(entry))
        if not ok:
            raise _win_error("Thread32First")
        while ok:
            if entry.th32OwnerProcessID == pid:
                found.append(entry.th32ThreadID)
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() not in (0, 18):  # ERROR_NO_MORE_FILES
            raise _win_error("Thread32Next")
        if len(found) != 1:
            raise ProcessTreeError(errno.EIO, "expected one suspended primary thread")
        thread = kernel.OpenThread(0x0002, False, found[0])  # THREAD_SUSPEND_RESUME
        if not thread:
            raise _win_error("OpenThread")
        previous = kernel.ResumeThread(thread)
        if previous != 1:
            if previous == 0xFFFFFFFF:
                raise _win_error("ResumeThread")
            raise ProcessTreeError(errno.EIO, "unexpected primary thread suspend count")
    finally:
        if thread:
            _win_close_handle(kernel, thread, "CloseHandle(thread)")
        _win_close_handle(kernel, snapshot, "CloseHandle(snapshot)")


class ManagedProcess:
    """Popen-like handle whose cleanup owns the complete child process tree."""

    def __init__(self, process: subprocess.Popen, *, job=None):
        self.process = process
        self._job = job
        self._closed = False
        self._operation_lock = threading.RLock()

    def __getattr__(self, name):
        return getattr(self.process, name)

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)

    def communicate(self, input=None, timeout=None):
        return self.process.communicate(input=input, timeout=timeout)

    def terminate_tree(self, grace_seconds: float = 1.0) -> None:
        try:
            valid = (not isinstance(grace_seconds, bool)
                     and isinstance(grace_seconds, (int, float))
                     and math.isfinite(grace_seconds) and 0 <= grace_seconds <= 3600)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("grace_seconds must be finite and non-negative")
        with self._operation_lock:
            if self._closed:
                return
            if IS_WINDOWS:
                if self._job:
                    if grace_seconds and self.poll() is None:
                        self.process.terminate()
                    deadline = time.monotonic() + float(grace_seconds)
                    while time.monotonic() < deadline and self._job_active_count() > 0:
                        time.sleep(max(0.0, min(0.025, deadline - time.monotonic())))
                    if self._job_active_count() > 0:
                        kernel = _win_kernel()
                        if not kernel.TerminateJobObject(self._job, 1):
                            raise _win_error("TerminateJobObject")
                    try:
                        self.process.wait(timeout=max(1.0, float(grace_seconds)))
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                elif self.poll() is None:
                    self.process.kill()
                    self.process.wait()
                return
            if self._posix_group_alive():
                number = signal.SIGKILL if grace_seconds == 0 else signal.SIGTERM
                try:
                    os.killpg(self.process.pid, number)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + float(grace_seconds)
                while (number == signal.SIGTERM and self._posix_group_alive()
                       and time.monotonic() < deadline):
                    self.process.poll()  # reap exited root before checking its group again
                    time.sleep(max(0.0, min(0.025, deadline - time.monotonic())))
                if number == signal.SIGTERM and self._posix_group_alive():
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            try:
                self.process.wait(timeout=max(1.0, float(grace_seconds)))
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def _posix_group_alive(self) -> bool:
        try:
            os.killpg(self.process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            return True

    def _job_active_count(self) -> int:  # pragma: no cover - Windows only
        kernel = _win_kernel()
        info = _AccountingInfo()
        returned = wintypes.DWORD()
        if not kernel.QueryInformationJobObject(self._job, 1, ctypes.byref(info),
                                               ctypes.sizeof(info), ctypes.byref(returned)):
            raise _win_error("QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def kill(self) -> None:
        self.terminate_tree(0)

    def terminate(self) -> None:
        self.terminate_tree(1.0)

    def close(self) -> None:
        with self._operation_lock:
            if self._closed:
                return
            try:
                if self._job is not None and self._job_active_count() > 0:
                    self.terminate_tree(0)
                elif self.poll() is None:
                    self.terminate_tree(0)
                elif not IS_WINDOWS and self._posix_group_alive():
                    self.terminate_tree(0)
            finally:
                if self._job:
                    _win_close_handle(_win_kernel(), self._job)
                    self._job = None
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


@contextmanager
def managed_process_tree(argv, **popen_kwargs) -> Iterator[ManagedProcess]:
    """Launch with descendants contained; context exit kills and reaps any survivor.

    On Windows the child starts suspended, is assigned to a kill-on-close Job
    Object, then its only primary thread is resumed. On POSIX a new session is
    the process-group boundary. `shell=True` is deliberately unsupported.
    """
    if popen_kwargs.pop("shell", False):
        raise ValueError("managed_process_tree does not support shell=True")
    if IS_WINDOWS:  # pragma: no cover - exercised by Windows CI
        kernel = _win_kernel()
        job = _win_create_job(kernel)
        flags = popen_kwargs.pop("creationflags", 0)
        popen_kwargs.pop("start_new_session", None)
        process = None
        try:
            process = subprocess.Popen(argv, creationflags=flags | 0x00000004,
                                       **popen_kwargs)  # CREATE_SUSPENDED
            if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(
                    _win_handle_value(process._handle))):
                raise _win_error("AssignProcessToJobObject")
            _win_resume_primary_thread(kernel, process.pid)
        except BaseException:
            if process is not None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            _win_close_handle(kernel, job, "CloseHandle(job after startup failure)")
            raise
        child = ManagedProcess(process, job=job)
        try:
            yield child
        finally:
            child.close()
    else:
        popen_kwargs.pop("creationflags", None)
        popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(argv, **popen_kwargs)
        child = ManagedProcess(process)
        try:
            yield child
        finally:
            child.close()
