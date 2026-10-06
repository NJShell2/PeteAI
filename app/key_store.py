"""Per-user encryption for the Purdue GenAI Studio API key.

The key is never stored in plaintext. Two platform backends:

- Windows: DPAPI via ctypes (``CryptProtectData``, current-user scope). No new
  dependency. Decryption only works for the logged-in Windows account on that
  machine, so a copied ``settings.json`` is useless anywhere else.
- Other platforms: Fernet (``cryptography`` lib) with a random 256-bit key kept
  in a 0600 file beside the data dir. This defeats casual reads (backups, share
  syncs, someone opening the JSON) but not a determined local attacker; it is
  documented as such rather than oversold.

Wire format: ``"<scheme>1:<base64>"`` where scheme is ``dpapi`` or ``fernet``.
A value with no recognised prefix is treated as legacy plaintext and migrated
(sealed and re-saved) on the next settings load.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from app.console import safe_print

_DPAPI_PREFIX = "dpapi1:"
_FERNET_PREFIX = "fernet1:"


def is_protected(blob: str | None) -> bool:
    return bool(blob) and (blob.startswith(_DPAPI_PREFIX) or blob.startswith(_FERNET_PREFIX))


# ---------------------------------------------------------------------------
# Windows: DPAPI via ctypes (no extra dependency)
# ---------------------------------------------------------------------------

def _dpapi_protect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    blob_in = DATA_BLOB()
    blob_in.cbData = len(data)
    buf = ctypes.create_string_buffer(data)
    blob_in.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    blob_out = DATA_BLOB()

    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    blob_in = DATA_BLOB()
    blob_in.cbData = len(data)
    buf = ctypes.create_string_buffer(data)
    blob_in.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    blob_out = DATA_BLOB()

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ---------------------------------------------------------------------------
# Other platforms: Fernet with a 0600 key file beside the data dir
# ---------------------------------------------------------------------------

def _fernet(data_dir: Path):
    from cryptography.fernet import Fernet  # lazy: only needed off-Windows

    key_file = data_dir / ".key"
    raw = None
    if key_file.exists():
        raw = key_file.read_bytes().strip()
    if not raw:
        raw = Fernet.generate_key()
        key_file.write_bytes(raw + b"\n")
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass  # best effort on filesystems without POSIX perms
    return Fernet(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def protect_api_key(plaintext: str, data_dir: Path) -> str:
    """Seal ``plaintext`` for the current user/machine. Returns the wire blob."""
    data = plaintext.encode("utf-8")
    if sys.platform == "win32":
        sealed = _dpapi_protect(data)
        return _DPAPI_PREFIX + base64.b64encode(sealed).decode("ascii")
    sealed = _fernet(data_dir).encrypt(data)
    return _FERNET_PREFIX + base64.b64encode(sealed).decode("ascii")


def unprotect_api_key(blob: str, data_dir: Path) -> str:
    """Unseal a blob produced by :func:`protect_api_key`. Returns "" on failure."""
    try:
        if blob.startswith(_DPAPI_PREFIX):
            if sys.platform != "win32":
                # A Windows-sealed blob copied to another OS is unrecoverable by
                # design; say so instead of crashing.
                safe_print("API key was sealed with Windows DPAPI and cannot be "
                           "decrypted on this platform.")
                return ""
            raw = base64.b64decode(blob[len(_DPAPI_PREFIX):])
            return _dpapi_unprotect(raw).decode("utf-8")
        if blob.startswith(_FERNET_PREFIX):
            raw = base64.b64decode(blob[len(_FERNET_PREFIX):])
            return _fernet(data_dir).decrypt(raw).decode("utf-8")
    except Exception as e:  # corrupted blob, rotated key file, etc.
        safe_print(f"Could not decrypt saved API key: {e}")
    return ""
