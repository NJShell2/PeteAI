"""Microsoft Graph authentication for PeteAI, via the OAuth2 device-code flow.

Why device code: a local desktop app has no fixed redirect URI, and asking the
user to register one is exactly the kind of setup friction that kills a
connector. With device flow the user just opens microsoft.com/devicelogin on
any device and types a short code -- the same pattern the Azure CLI uses.

What the user needs (one time): an Azure app registration (portal.azure.com ->
App registrations -> New). Supported account types: "Accounts in any
organizational directory and personal Microsoft accounts". No redirect URI is
needed. Under Authentication, enable "Allow public client flows". Then paste
the Application (client) ID into Pete's Settings.

Tokens are sealed with the same per-user encryption as the API key
(app/key_store.py): DPAPI on Windows, Fernet elsewhere. The refresh token
lets Pete stay signed in without bothering the user again.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.console import safe_print
from app.key_store import protect_api_key, unprotect_api_key

# Least privilege for what Pete's tools actually do.
SCOPES = [
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Chat.ReadWrite",
    "offline_access",
]

TOKEN_FILE = "graph_tokens.json"


def _authority(tenant: str) -> str:
    tenant = (tenant or "common").strip() or "common"
    return f"https://login.microsoftonline.com/{tenant}"


def token_path(data_dir: Path) -> Path:
    return Path(data_dir) / TOKEN_FILE


async def start_device_flow(client_id: str, tenant: str = "common") -> Dict[str, Any]:
    """Begins the device-code flow. Returns the user code + verification URI."""
    client_id = (client_id or "").strip()
    if not client_id:
        raise ValueError("No Microsoft client ID configured. Paste your Azure app's "
                         "Application (client) ID into Pete's Settings first.")
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{_authority(tenant)}/oauth2/v2.0/devicecode",
            data={"client_id": client_id, "scope": " ".join(SCOPES)},
        )
        resp.raise_for_status()
        data = resp.json()
    return {
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri") or data.get("verification_url"),
        "device_code": data["device_code"],
        "expires_in": data.get("expires_in", 900),
        "interval": data.get("interval", 5),
        "message": data.get("message", ""),
    }


class AuthorizationPending(Exception):
    """The user hasn't approved the device-code request yet."""


async def poll_device_flow(client_id: str, device_code: str,
                         tenant: str = "common") -> Dict[str, Any]:
    """Polls the token endpoint once. Raises AuthorizationPending until approved."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{_authority(tenant)}/oauth2/v2.0/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code,
            },
        )
    if resp.status_code == 400:
        err = (resp.json().get("error") or "")
        if err == "authorization_pending":
            raise AuthorizationPending()
        raise RuntimeError(f"Microsoft sign-in failed: {err or resp.text[:200]}")
    resp.raise_for_status()
    return resp.json()


def save_tokens(data_dir: Path, token_response: Dict[str, Any]) -> Dict[str, Any]:
    """Seals and stores the token response. Returns the stored record (no secrets)."""
    record = {
        "access_token": token_response["access_token"],
        "refresh_token": token_response.get("refresh_token", ""),
        "expires_at": time.time() + int(token_response.get("expires_in", 3600)),
        "scope": token_response.get("scope", ""),
        "saved_at": time.time(),
    }
    blob = protect_api_key(json.dumps(record), Path(data_dir))
    token_path(data_dir).write_text(json.dumps({"sealed": blob}), encoding="utf-8")
    try:
        token_path(data_dir).chmod(0o600)
    except Exception:
        pass
    return {"connected": True}


def load_token_record(data_dir: Path) -> Optional[Dict[str, Any]]:
    """Reads and unseals the stored tokens. None when never connected."""
    path = token_path(data_dir)
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8")).get("sealed", "")
        return json.loads(unprotect_api_key(blob, Path(data_dir)))
    except Exception as e:
        safe_print(f"Could not read Microsoft tokens: {e}")
        return None


def disconnect(data_dir: Path) -> None:
    try:
        token_path(data_dir).unlink()
    except FileNotFoundError:
        pass


async def _refresh(client_id: str, refresh_token: str, tenant: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{_authority(tenant)}/oauth2/v2.0/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
                "scope": " ".join(SCOPES),
            },
        )
        resp.raise_for_status()
        return resp.json()


async def get_access_token(data_dir: Path, client_id: str,
                         tenant: str = "common") -> Optional[str]:
    """A usable access token, refreshing silently when needed. None if signed out."""
    record = load_token_record(data_dir)
    if not record or not record.get("access_token"):
        return None
    # Refresh a little early so a token never dies mid-request.
    if record.get("expires_at", 0) - time.time() > 300:
        return record["access_token"]
    refresh_token = record.get("refresh_token") or ""
    if not refresh_token:
        return None
    try:
        fresh = await _refresh(client_id, refresh_token, tenant)
    except Exception as e:
        safe_print(f"Microsoft token refresh failed: {e}")
        return None
    # A refresh response may omit a new refresh token; keep the old one then.
    if not fresh.get("refresh_token"):
        fresh["refresh_token"] = refresh_token
    save_tokens(data_dir, fresh)
    return fresh["access_token"]


def connection_status(data_dir: Path) -> Dict[str, Any]:
    record = load_token_record(data_dir)
    if not record:
        return {"connected": False}
    return {
        "connected": True,
        "expires_in": max(0, int(record.get("expires_at", 0) - time.time())),
    }
