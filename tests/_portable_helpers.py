"""Small platform-neutral helpers for tests that need OS-specific behavior."""

from __future__ import annotations

from contextlib import contextmanager
import os
import builtins
import sqlite3
from pathlib import Path
from unittest.mock import patch


@contextmanager
def sqlite_connection(database):
    """Commit/rollback like sqlite's connection context and always close it."""
    connection = sqlite3.connect(database)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def isolated_home_env(base, home):
    """Return a child environment whose home resolves to a disposable test path."""
    environment = dict(base)
    environment["HOME"] = str(home)
    if os.name == "nt":
        drive, tail = os.path.splitdrive(str(Path(home).resolve()))
        environment["USERPROFILE"] = str(Path(home).resolve())
        if drive:
            environment["HOMEDRIVE"] = drive
            environment["HOMEPATH"] = tail or "\\"
    return environment


def readable_hook_command(command):
    """Expose argv text from the deterministic Windows hook serializer for assertions."""
    if os.name == "nt" and "-EncodedCommand " in command:
        import base64
        script = base64.b64decode(command.split()[-1]).decode("utf-16le")
        return script.replace("'", "")
    return command


def process_is_gone(pid):
    """Check a PID without using signal 0 on Windows, where it is not portable."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    # PROCESS_QUERY_LIMITED_INFORMATION is sufficient and avoids requesting
    # rights irrelevant to this assertion.
    handle = open_process(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: no such PID
    try:
        code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(code)):
            return False
        return code.value != 259  # STILL_ACTIVE
    finally:
        close_handle(handle)


def deny_path_access(testcase, target, *, directory=False):
    """Inject a deterministic access denial at pathlib's read/traversal boundary."""
    target = Path(target).resolve()
    original_open = Path.open
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text
    original_iterdir = Path.iterdir
    original_access = os.access
    original_scandir = os.scandir
    original_builtin_open = builtins.open

    def matches(path):
        path = Path(path).resolve()
        return path == target or (directory and target in path.parents)

    def denied(path):
        raise PermissionError(13, "test-injected access denial", os.fspath(path))

    def denied_open(path, *args, **kwargs):
        if matches(path):
            denied(path)
        return original_open(path, *args, **kwargs)

    def denied_read_bytes(path, *args, **kwargs):
        if matches(path):
            denied(path)
        return original_read_bytes(path, *args, **kwargs)

    def denied_read_text(path, *args, **kwargs):
        if matches(path):
            denied(path)
        return original_read_text(path, *args, **kwargs)

    def denied_iterdir(path, *args, **kwargs):
        if directory and Path(path).resolve() == target:
            denied(path)
        return original_iterdir(path, *args, **kwargs)

    def denied_scandir(path):
        if directory and matches(path):
            denied(path)
        return original_scandir(path)

    def denied_access(path, *args, **kwargs):
        if matches(path):
            return False
        return original_access(path, *args, **kwargs)

    def denied_builtin_open(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and matches(path):
            denied(path)
        return original_builtin_open(path, *args, **kwargs)

    for name, replacement in (("open", denied_open), ("read_bytes", denied_read_bytes),
                              ("read_text", denied_read_text), ("iterdir", denied_iterdir)):
        cleanup = patch.object(Path, name, replacement)
        cleanup.start()
        testcase.addCleanup(cleanup.stop)
    access_cleanup = patch.object(os, "access", denied_access)
    access_cleanup.start()
    testcase.addCleanup(access_cleanup.stop)
    scandir_cleanup = patch.object(os, "scandir", denied_scandir)
    scandir_cleanup.start()
    testcase.addCleanup(scandir_cleanup.stop)
    builtin_cleanup = patch.object(builtins, "open", denied_builtin_open)
    builtin_cleanup.start()
    testcase.addCleanup(builtin_cleanup.stop)


def assert_private_path(testcase, path, *, directory=False):
    """Check Unix private mode bits or the Windows protected private DACL."""
    path = Path(path)
    if os.name != "nt":
        import stat
        expected = 0o700 if directory else 0o600
        testcase.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)
        return

    import ctypes
    from ctypes import wintypes

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD),
                    ("AclBytesFree", wintypes.DWORD)]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte),
                    ("AceSize", wintypes.WORD)]

    class ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [("Header", ACE_HEADER), ("Mask", wintypes.DWORD),
                    ("SidStart", wintypes.DWORD)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_named_security = advapi32.GetNamedSecurityInfoW
    get_named_security.argtypes = (wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(ctypes.c_void_p))
    get_named_security.restype = wintypes.DWORD
    get_control = advapi32.GetSecurityDescriptorControl
    get_control.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                            ctypes.POINTER(wintypes.DWORD))
    get_control.restype = wintypes.BOOL
    get_acl_info = advapi32.GetAclInformation
    get_acl_info.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int)
    get_acl_info.restype = wintypes.BOOL
    get_ace = advapi32.GetAce
    get_ace.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    get_ace.restype = wintypes.BOOL
    sid_to_string = advapi32.ConvertSidToStringSidW
    sid_to_string.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    sid_to_string.restype = wintypes.BOOL
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.HANDLE))
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    get_token_information.restype = wintypes.BOOL
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p

    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    sacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = get_named_security(str(path), 1, 0x00000004, None,
                                ctypes.byref(group), ctypes.byref(dacl), ctypes.byref(sacl),
                                ctypes.byref(descriptor))
    testcase.assertEqual(status, 0, f"GetNamedSecurityInfoW failed: {status}")
    try:
        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        class TOKEN_USER(ctypes.Structure):
            _fields_ = [("User", SID_AND_ATTRIBUTES)]

        token = wintypes.HANDLE()
        testcase.assertTrue(open_process_token(get_current_process(), 0x0008,
                                               ctypes.byref(token)),
                            f"OpenProcessToken failed: {ctypes.get_last_error()}")
        try:
            token_bytes = wintypes.DWORD()
            get_token_information(token, 1, None, 0, ctypes.byref(token_bytes))
            testcase.assertGreater(token_bytes.value, 0,
                                   f"GetTokenInformation size failed: {ctypes.get_last_error()}")
            token_user_buffer = ctypes.create_string_buffer(token_bytes.value)
            testcase.assertTrue(get_token_information(
                token, 1, token_user_buffer, token_bytes, ctypes.byref(token_bytes)),
                f"GetTokenInformation failed: {ctypes.get_last_error()}")
            user_sid = ctypes.cast(token_user_buffer, ctypes.POINTER(TOKEN_USER)).contents.User.Sid
            user_text = wintypes.LPWSTR()
            testcase.assertTrue(sid_to_string(user_sid, ctypes.byref(user_text)),
                                f"ConvertSidToStringSidW failed: {ctypes.get_last_error()}")
            try:
                token_user_sid = user_text.value
            finally:
                local_free(ctypes.cast(user_text, ctypes.c_void_p))
        finally:
            testcase.assertTrue(close_handle(token), "CloseHandle(token) failed")
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        testcase.assertTrue(get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)))
        testcase.assertTrue(control.value & 0x1000, "private DACL must block inherited access")
        info = ACL_SIZE_INFORMATION()
        testcase.assertTrue(get_acl_info(dacl, ctypes.byref(info), ctypes.sizeof(info), 2))
        testcase.assertEqual(info.AceCount, 2, "private DACL must contain exactly two ACEs")
        principals = set()
        for index in range(info.AceCount):
            ace_ptr = ctypes.c_void_p()
            testcase.assertTrue(get_ace(dacl, index, ctypes.byref(ace_ptr)))
            header = ctypes.cast(ace_ptr, ctypes.POINTER(ACE_HEADER)).contents
            testcase.assertEqual(header.AceType, 0, "private DACL may contain only allow ACEs")
            testcase.assertEqual(header.AceFlags, 0x03 if directory else 0,
                                 "private DACL ACE inheritance flags differ")
            ace = ctypes.cast(ace_ptr, ctypes.POINTER(ACCESS_ALLOWED_ACE)).contents
            sid = ctypes.c_void_p(ace_ptr.value + ACCESS_ALLOWED_ACE.SidStart.offset)
            sid_text = wintypes.LPWSTR()
            testcase.assertTrue(sid_to_string(sid, ctypes.byref(sid_text)))
            try:
                principal = sid_text.value
            finally:
                local_free(ctypes.cast(sid_text, ctypes.c_void_p))
            principals.add(principal)
            testcase.assertEqual(ace.Mask, 0x001F01FF,
                                 f"principal {principal} must have exactly full private access")
        testcase.assertEqual(principals, {token_user_sid, "S-1-5-18"})
    finally:
        local_free(descriptor)
