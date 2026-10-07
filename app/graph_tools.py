"""Microsoft Graph tools: Outlook mail, Teams chats, Calendar events.

Every function returns either the requested data or a plain-English string
explaining what is missing ("not connected", "needs a client ID"). The agent
passes those strings straight to the model, which is why they read like
instructions rather than error codes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app import graph_auth
from app.console import safe_print

GRAPH = "https://graph.microsoft.com/v1.0"

NOT_CONNECTED = (
    "Microsoft is not connected. Tell the user to open Pete's Settings, paste their "
    "Azure app's Application (client) ID, and press Connect Microsoft."
)
NO_CLIENT_ID = (
    "No Microsoft client ID is configured. Tell the user to paste their Azure app's "
    "Application (client) ID into Pete's Settings first."
)


async def _token(data_dir: Path, client_id: str, tenant: str) -> Optional[str]:
    if not (client_id or "").strip():
        return None
    return await graph_auth.get_access_token(Path(data_dir), client_id, tenant or "common")


async def _call(method: str, path: str, data_dir: Path, client_id: str, tenant: str,
               params: Optional[Dict[str, Any]] = None,
               json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One Graph call with a single silent retry after a token refresh."""
    token = await _token(data_dir, client_id, tenant)
    if not token:
        return {"_error": NOT_CONNECTED if (client_id or "").strip() else NO_CLIENT_ID}
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.request(method, GRAPH + path, headers=headers,
                                    params=params, json=json_body)
        if resp.status_code == 401:
            # Token died between the check and the call: refresh once, retry once.
            token = await graph_auth.get_access_token(Path(data_dir), client_id,
                                                      tenant or "common", )
            if token:
                headers["Authorization"] = f"Bearer {token}"
                resp = await client.request(method, GRAPH + path, headers=headers,
                                            params=params, json=json_body)
        if resp.status_code in (401, 403):
            return {"_error": f"Microsoft refused the request ({resp.status_code}). "
                              "The sign-in may have expired -- reconnect in Settings."}
        resp.raise_for_status()
        if resp.status_code == 202 or not resp.content:
            return {"_ok": True}
        return resp.json()


def _err(result: Dict[str, Any]) -> Optional[str]:
    return result.get("_error")


# ---------------------------------------------------------------------------
# Outlook mail
# ---------------------------------------------------------------------------

def _fmt_message(m: Dict[str, Any]) -> Dict[str, Any]:
    sender = (m.get("from") or {}).get("emailAddress") or {}
    return {
        "id": m.get("id"),
        "subject": m.get("subject") or "(no subject)",
        "from": f"{sender.get('name', '')} <{sender.get('address', '')}>".strip(),
        "received": m.get("receivedDateTime"),
        "is_read": m.get("isRead"),
        "preview": m.get("bodyPreview") or "",
    }


async def read_email(data_dir: Path, client_id: str, tenant: str,
                     count: int = 10) -> Any:
    """Newest inbox messages."""
    count = max(1, min(int(count or 10), 25))
    result = await _call("GET", "/me/messages", data_dir, client_id, tenant, params={
        "$top": count, "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead",
    })
    if _err(result):
        return result["_error"]
    return [_fmt_message(m) for m in result.get("value", [])] or "The inbox is empty."


async def search_email(data_dir: Path, client_id: str, tenant: str,
                       query: str, count: int = 10) -> Any:
    """Searches mail. ``query`` supports keywords and from: filters."""
    query = (query or "").strip()
    if not query:
        return "No search query given."
    count = max(1, min(int(count or 10), 25))
    result = await _call("GET", "/me/messages", data_dir, client_id, tenant, params={
        "$search": f'"{query}"', "$top": count,
        "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead",
    })
    if _err(result):
        return result["_error"]
    found = [_fmt_message(m) for m in result.get("value", [])]
    return found or f"No messages matched '{query}'."


async def send_email(data_dir: Path, client_id: str, tenant: str, to: str,
                     subject: str, body: str) -> Any:
    """Sends an email from the connected account."""
    to = (to or "").strip()
    if not to:
        return "No recipient given."
    recipients = [{"emailAddress": {"address": a.strip()}}
                  for a in to.split(",") if a.strip()]
    result = await _call("POST", "/me/sendMail", data_dir, client_id, tenant,
                         json_body={
                             "message": {
                                 "subject": subject or "",
                                 "body": {"contentType": "Text", "content": body or ""},
                                 "toRecipients": recipients,
                             },
                             "saveToSentItems": True,
                         })
    if _err(result):
        return result["_error"]
    return f"Email sent to {to}."


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def _fmt_event(e: Dict[str, Any]) -> Dict[str, Any]:
    start = (e.get("start") or {})
    end = (e.get("end") or {})
    return {
        "id": e.get("id"),
        "subject": e.get("subject") or "(no title)",
        "start": start.get("dateTime"),
        "end": end.get("dateTime"),
        "location": ((e.get("location") or {}).get("displayName") or ""),
        "is_all_day": e.get("isAllDay"),
    }


async def list_calendar_events(data_dir: Path, client_id: str, tenant: str,
                               days: int = 7) -> Any:
    """Upcoming events, default next 7 days."""
    days = max(1, min(int(days or 7), 60))
    now = datetime.now(timezone.utc)
    result = await _call("GET", "/me/calendarview", data_dir, client_id, tenant, params={
        "startDateTime": now.isoformat(),
        "endDateTime": (now + timedelta(days=days)).isoformat(),
        "$orderby": "start/dateTime", "$top": 50,
        "$select": "id,subject,start,end,location,isAllDay",
    })
    if _err(result):
        return result["_error"]
    events = [_fmt_event(e) for e in result.get("value", [])]
    return events or "No upcoming events."


async def create_calendar_event(data_dir: Path, client_id: str, tenant: str,
                                subject: str, start: str, end: str,
                                attendees: str = "", location: str = "") -> Any:
    """Creates an event. ``start``/``end``: ISO-ish datetimes, e.g. 2026-10-08T14:00."""
    subject = (subject or "").strip() or "(no title)"
    if not start or not end:
        return "I need both a start and an end time to create the event."
    event: Dict[str, Any] = {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
    }
    if location:
        event["location"] = {"displayName": location}
    if attendees:
        event["attendees"] = [
            {"emailAddress": {"address": a.strip()}, "type": "required"}
            for a in attendees.split(",") if a.strip()
        ]
    result = await _call("POST", "/me/events", data_dir, client_id, tenant,
                         json_body=event)
    if _err(result):
        return result["_error"]
    return f"Created '{subject}' from {start} to {end}."


# ---------------------------------------------------------------------------
# Teams chats
# ---------------------------------------------------------------------------

async def list_teams_chats(data_dir: Path, client_id: str, tenant: str,
                           count: int = 15) -> Any:
    """Recent Teams chats (1:1 and group)."""
    count = max(1, min(int(count or 15), 30))
    result = await _call("GET", "/me/chats", data_dir, client_id, tenant, params={
        "$top": count, "$orderby": "lastUpdatedDateTime desc",
        "$select": "id,topic,chatType,lastUpdatedDateTime",
    })
    if _err(result):
        return result["_error"]
    chats = [{
        "id": c.get("id"),
        "topic": c.get("topic") or f"({c.get('chatType', 'chat')})",
        "type": c.get("chatType"),
        "updated": c.get("lastUpdatedDateTime"),
    } for c in result.get("value", [])]
    return chats or "No Teams chats found."


async def send_teams_message(data_dir: Path, client_id: str, tenant: str,
                             chat_id: str, message: str) -> Any:
    """Sends a message to a Teams chat (use list_teams_chats for the id)."""
    if not (chat_id or "").strip():
        return "No chat id given. List the Teams chats first and pick one."
    if not (message or "").strip():
        return "No message text given."
    result = await _call("POST", f"/me/chats/{chat_id}/messages",
                         data_dir, client_id, tenant,
                         json_body={"body": {"contentType": "text",
                                             "content": message.strip()}})
    if _err(result):
        return result["_error"]
    return "Teams message sent."
