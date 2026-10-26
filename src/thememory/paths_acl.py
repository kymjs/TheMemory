"""Permission / ACL helpers for ~/.thememory (spec §4 / §15.1)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def ensure_unix_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def check_path_privacy(path: Path) -> dict[str, Any]:
    """Return {ok, detail}. On Windows checks current-user-only ACL; elsewhere mode bits."""
    if not path.exists():
        return {"ok": False, "detail": "missing"}
    if sys.platform == "win32":
        return _windows_acl_ok(path)
    try:
        st = path.stat()
        mode = st.st_mode & 0o777
        # files should be 0600; dirs 0700. Warn if group/other have any bits.
        if mode & 0o077:
            return {"ok": False, "detail": f"mode={oct(mode)} allows group/other"}
        return {"ok": True, "detail": oct(mode)}
    except OSError as exc:
        return {"ok": False, "detail": str(exc)}


def _windows_acl_ok(path: Path) -> dict[str, Any]:
    try:
        import ctypes
        from ctypes import wintypes

        GetFileSecurity = ctypes.windll.advapi32.GetFileSecurityW
        GetSecurityDescriptorDacl = ctypes.windll.advapi32.GetSecurityDescriptorDacl
        GetAclInformation = ctypes.windll.advapi32.GetAclInformation
        GetAce = ctypes.windll.advapi32.GetAce

        OWNER_SECURITY_INFORMATION = 0x00000001
        DACL_SECURITY_INFORMATION = 0x00000004
        size_needed = wintypes.DWORD(0)
        GetFileSecurity(
            str(path),
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            None,
            0,
            ctypes.byref(size_needed),
        )
        if size_needed.value <= 0:
            return {"ok": False, "detail": "GetFileSecurity size=0"}
        buf = ctypes.create_string_buffer(size_needed.value)
        if not GetFileSecurity(
            str(path),
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            buf,
            size_needed,
            ctypes.byref(size_needed),
        ):
            return {"ok": False, "detail": f"GetFileSecurity failed err={ctypes.GetLastError()}"}
        # Presence of a DACL is enough for a soft check; full SID walk is best-effort.
        p_dacl = ctypes.c_void_p()
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        if not GetSecurityDescriptorDacl(
            buf, ctypes.byref(present), ctypes.byref(p_dacl), ctypes.byref(defaulted)
        ):
            return {"ok": False, "detail": "GetSecurityDescriptorDacl failed"}
        if not present.value:
            return {"ok": False, "detail": "no DACL (null allows everyone)"}
        # Count ACEs — non-zero DACL present is the baseline Windows check.
        class ACL_SIZE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("AceCount", wintypes.DWORD),
                ("AclBytesInUse", wintypes.DWORD),
                ("AclBytesFree", wintypes.DWORD),
            ]

        info = ACL_SIZE_INFORMATION()
        AclSizeInformation = 2
        if p_dacl and GetAclInformation(
            p_dacl, ctypes.byref(info), ctypes.sizeof(info), AclSizeInformation
        ):
            return {"ok": True, "detail": f"dacl_aces={info.AceCount}"}
        return {"ok": True, "detail": "dacl_present"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"acl_probe_failed: {exc}"}
