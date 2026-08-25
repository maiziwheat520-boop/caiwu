"""Read-only Windows Credential Manager access for isolated staging."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class CredentialStoreError(RuntimeError):
    """The local credential cannot be read safely."""


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialTokenProvider:
    """Resolve one generic credential without exposing it to callers/logs."""

    def __init__(self, target: str) -> None:
        if not isinstance(target, str) or not target.strip() or len(target) > 512:
            raise ValueError("credential target is invalid")
        self._target = target

    def get_access_token(self) -> str:
        if os.name != "nt":
            raise CredentialStoreError("Windows Credential Manager is unavailable")
        advapi32 = ctypes.WinDLL("Advapi32.dll")
        cred_read = advapi32.CredReadW
        cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        cred_read.restype = wintypes.BOOL
        cred_free = advapi32.CredFree
        cred_free.argtypes = [ctypes.c_void_p]
        cred_free.restype = wintypes.BOOL
        credential_ptr = ctypes.c_void_p()
        if not cred_read(self._target, 1, 0, ctypes.byref(credential_ptr)):
            raise CredentialStoreError("staging credential is unavailable")
        try:
            credential = ctypes.cast(credential_ptr, ctypes.POINTER(_Credential)).contents
            size = int(credential.CredentialBlobSize)
            if size <= 0 or size > 16 * 1024 or not credential.CredentialBlob:
                raise CredentialStoreError("staging credential payload is invalid")
            raw = ctypes.string_at(credential.CredentialBlob, size)
            token = _decode_blob(raw)
            if not token or any(ord(char) == 0 for char in token):
                raise CredentialStoreError("staging credential payload is invalid")
            return token
        finally:
            cred_free(credential_ptr)


def _decode_blob(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-16-le"):
        try:
            value = raw.decode(encoding).rstrip("\x00")
        except UnicodeDecodeError:
            continue
        if value.strip():
            return value
    raise CredentialStoreError("staging credential encoding is invalid")
