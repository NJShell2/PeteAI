import json
import secrets
import time
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query, Body, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import load_settings, save_settings, AppSettings, BASE_DIR, DATA_DIR, get_api_key
from app.key_store import is_protected, protect_api_key
from app.storage import storage, Message, Chat
from app.agent import pete_agent
from app.browser_tool import browser_tool
from app.browser_session import browser_session
from app.llm_client import llm_client

app = FastAPI(title="Pete AI", description="Purdue Pete AI: Solo Agent with Subchats & Pete Squad Multi-Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request Models
class CreateChatRequest(BaseModel):
    title: Optional[str] = "New Chat"
    parent_id: Optional[str] = None
    workspace_id: Optional[str] = None
    model: Optional[str] = None

class MessageRequest(BaseModel):
    content: str
    model: Optional[str] = None
    agent_mode: Optional[str] = "solo" # "solo" or "squad"

class SearchRequest(BaseModel):
    query: str
    max_results: Optional[int] = 5

class BrowseRequest(BaseModel):
    url: str

class ScreenshotRequest(BaseModel):
    url: str
    workspace_id: str
    filename: Optional[str] = None

class BrowserActionRequest(BaseModel):
    action: str
    ref: Optional[int] = None
    text: Optional[str] = None
    url: Optional[str] = None
    key: Optional[str] = None
    direction: Optional[str] = None
    amount: Optional[int] = None
    ms: Optional[int] = None
    value: Optional[str] = None
    submit: Optional[bool] = None

class BrowserTaskRequest(BaseModel):
    goal: str
    chat_id: Optional[str] = None
    model: Optional[str] = None
    max_steps: Optional[int] = None

class SaveFileRequest(BaseModel):
    content: str

class CreateFileRequest(BaseModel):
    filename: str
    content: Optional[str] = ""

# Settings API
def _public_settings() -> dict:
    """Settings safe to send to the browser: the API key is never included.

    The frontend learns only whether a key is saved; the key itself is
    unsealed server-side only at the moment it is used.
    """
    settings = load_settings()
    data = settings.model_dump(exclude={"purdue_api_key"})
    data["has_api_key"] = bool(get_api_key(settings))
    return data

@app.get("/api/settings")
async def get_settings():
    return _public_settings()

@app.post("/api/settings")
async def update_settings(settings: AppSettings):
    existing = load_settings()
    incoming = (settings.purdue_api_key or "").strip()
    if incoming and not is_protected(incoming):
        # A fresh key pasted in the UI: seal it before it touches the disk.
        settings.purdue_api_key = protect_api_key(incoming, DATA_DIR)
    elif not incoming:
        # Blank key field means "keep the saved key", not "delete it".
        settings.purdue_api_key = existing.purdue_api_key
    # else: an already-sealed blob passed through (not expected from the UI,
    # but never downgrade it to plaintext).
    save_settings(settings)
    return {"status": "success", "settings": _public_settings()}

@app.delete("/api/settings/api-key")
async def forget_api_key():
    settings = load_settings()
    settings.purdue_api_key = ""
    save_settings(settings)
    return {"status": "success", "settings": _public_settings()}

@app.post("/api/settings/test-connection")
async def test_connection(payload: Dict[str, Any] = Body(default={})):
    """Validate a candidate key against Studio WITHOUT saving it.

    Falls back to the saved key when none is supplied. ``strict=True`` on the
    model listing so a failure is reported honestly instead of being masked by
    the offline fallback list.
    """
    url = payload.get("purdue_api_url") or load_settings().purdue_api_url
    candidate = (payload.get("purdue_api_key") or "").strip()
    if candidate and is_protected(candidate):
        # A sealed blob is not a usable credential; unseal first.
        candidate = get_api_key(load_settings())
    key = candidate or get_api_key()
    if not key:
        return {"ok": False, "error": "No API key provided and none is saved."}
    try:
        models = await llm_client.list_models(api_key=key, base_url=url, strict=True)
        return {"ok": True, "models": models}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# Microsoft Graph connector (Outlook, Teams, Calendar) ---------------------------
# Device-code flow: the user opens microsoft.com/devicelogin on any device and
# types the short code. No redirect URI, no localhost callback -- the easiest
# setup that is still real OAuth.

# Pending device flows, kept server-side: the browser only ever sees the
# user-facing code, never the device_code credential. Entries expire.
_graph_pending: Dict[str, Dict[str, Any]] = {}


def _prune_graph_pending() -> None:
    now = time.time()
    for sid in [k for k, v in _graph_pending.items()
                if now - v.get("started", 0) > 15 * 60]:
        _graph_pending.pop(sid, None)


@app.get("/api/graph/status")
async def graph_status():
    from app import graph_auth
    from app.config import DATA_DIR
    settings = load_settings()
    status = graph_auth.connection_status(DATA_DIR)
    status["has_client_id"] = bool((settings.graph_client_id or "").strip())
    return status


@app.post("/api/graph/connect")
async def graph_connect():
    """Starts the device-code flow. Returns the code the user types at microsoft.com/devicelogin."""
    from app import graph_auth
    settings = load_settings()
    try:
        flow = await graph_auth.start_device_flow(
            settings.graph_client_id, settings.graph_tenant or "common")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Microsoft: {e}")
    _prune_graph_pending()
    session_id = secrets.token_urlsafe(16)
    _graph_pending[session_id] = {
        "device_code": flow["device_code"],
        "started": time.time(),
    }
    return {
        "session_id": session_id,
        "user_code": flow["user_code"],
        "verification_uri": flow["verification_uri"],
        "message": flow["message"],
        "interval": flow["interval"],
    }


@app.post("/api/graph/poll")
async def graph_poll(payload: Dict[str, Any] = Body(default={})):
    """Polls once for the device-code approval. The UI calls this every few seconds."""
    from app import graph_auth
    from app.config import DATA_DIR
    from app.graph_auth import AuthorizationPending
    settings = load_settings()
    _prune_graph_pending()
    session_id = (payload.get("session_id") or "").strip()
    pending = _graph_pending.get(session_id)
    if not pending:
        raise HTTPException(status_code=400, detail="Sign-in expired. Start over with Connect.")
    try:
        tokens = await graph_auth.poll_device_flow(
            settings.graph_client_id, pending["device_code"],
            settings.graph_tenant or "common")
    except AuthorizationPending:
        return {"status": "pending"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    graph_auth.save_tokens(DATA_DIR, tokens)
    _graph_pending.pop(session_id, None)
    return {"status": "done", **graph_auth.connection_status(DATA_DIR)}


@app.post("/api/graph/disconnect")
async def graph_disconnect():
    from app import graph_auth
    from app.config import DATA_DIR
    graph_auth.disconnect(DATA_DIR)
    return {"status": "success", "connected": False}


# Models API
@app.get("/api/models")
async def get_models():
    try:
        models = await llm_client.list_models()
        return {"models": models, "error": None}
    except RuntimeError as e:
        # No key configured: say so plainly instead of serving stale fallbacks.
        return {"models": [], "error": str(e)}

# Chats & Subchats API
@app.get("/api/chats")
async def list_chats():
    tree = storage.get_chat_tree()
    return {"chats": tree}

@app.post("/api/chats")
async def create_chat(req: CreateChatRequest):
    chat = storage.create_chat(
        title=req.title or "New Chat",
        parent_id=req.parent_id,
        workspace_id=req.workspace_id,
        model=req.model
    )
    return chat.model_dump()

@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str):
    chat = storage.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    subchats = storage.get_subchats(chat_id)
    breadcrumbs = storage.get_breadcrumbs(chat_id)
    chat_dict = chat.model_dump()
    chat_dict["subchats"] = [s.model_dump() for s in subchats]
    chat_dict["breadcrumbs"] = breadcrumbs
    return chat_dict

@app.get("/api/chats/{chat_id}/breadcrumbs")
async def get_chat_breadcrumbs(chat_id: str):
    crumbs = storage.get_breadcrumbs(chat_id)
    return {"breadcrumbs": crumbs}

@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str):
    success = storage.delete_chat(chat_id)
    if not success:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"status": "deleted"}

# Subchat Synthesis API
@app.post("/api/chats/{subchat_id}/synthesize")
async def synthesize_subchat(subchat_id: str):
    msg = await pete_agent.synthesize_subchat(subchat_id)
    if not msg:
        raise HTTPException(status_code=400, detail="Cannot synthesize this chat (it may not be a subchat or parent was deleted)")
    return {"status": "success", "message": msg.model_dump()}

# Stream Message Endpoint (Server-Sent Events)
@app.post("/api/chats/{chat_id}/message")
async def send_message(chat_id: str, req: MessageRequest):
    chat = storage.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    async def event_generator():
        try:
            async for event in pete_agent.run(chat_id, req.content, model_override=req.model, agent_mode=req.agent_mode or "solo"):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# Workspace Files API
@app.get("/api/workspaces/{workspace_id}/files")
async def list_workspace_files(workspace_id: str):
    files = storage.list_files(workspace_id)
    return {"files": [f.model_dump() for f in files]}

@app.post("/api/workspaces/{workspace_id}/files")
async def create_new_file(workspace_id: str, req: CreateFileRequest):
    msg = storage.write_workspace_text_file(workspace_id, req.filename, req.content or "")
    return {"status": "success", "message": msg}

@app.put("/api/workspaces/{workspace_id}/files/{filename}")
async def update_workspace_file_content(workspace_id: str, filename: str, req: SaveFileRequest):
    msg = storage.write_workspace_text_file(workspace_id, filename, req.content)
    return {"status": "success", "message": msg}

@app.post("/api/workspaces/{workspace_id}/upload")
async def upload_workspace_file(workspace_id: str, file: UploadFile = File(...)):
    content = await file.read()
    info = storage.save_workspace_file(workspace_id, file.filename, content)
    return info.model_dump()

@app.get("/api/workspaces/{workspace_id}/files/{filename}")
async def get_workspace_file(workspace_id: str, filename: str):
    file_path = storage.get_workspace_path(workspace_id) / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=str(file_path), filename=filename)

@app.delete("/api/workspaces/{workspace_id}/files/{filename}")
async def delete_workspace_file(workspace_id: str, filename: str):
    success = storage.delete_workspace_file(workspace_id, filename)
    if not success:
        raise HTTPException(status_code=404, detail="File not found")
    return {"status": "deleted"}

# Browser Direct Utilities
@app.post("/api/browser/search")
async def browser_search(req: SearchRequest):
    results = await browser_tool.search_web(req.query, req.max_results or 5)
    return {"results": results}

@app.post("/api/browser/browse")
async def browser_browse(req: BrowseRequest):
    result = await browser_tool.browse_page(req.url)
    return result

@app.post("/api/browser/screenshot")
async def browser_screenshot(req: ScreenshotRequest):
    result = await browser_tool.take_screenshot(req.url, req.workspace_id, req.filename)
    return result

# ── Live browser session ────────────────────────────────────────────────────────
# The agent and the user drive one shared page. These endpoints back the live
# viewport: a WebSocket for frames, plus REST for manual control and the goal loop.

@app.websocket("/api/browser/live/{chat_id}")
async def browser_live(websocket: WebSocket, chat_id: str):
    """Streams JPEG frames of the shared page to one viewer, and reads input back.

    The socket is bidirectional. Frames and status flow out; mouse, wheel and
    keyboard events flow in and are replayed into the same page, which is what
    turns the viewport from a video into something you can actually click in.

    Input is only honoured while the user actually holds control. Without that
    gate the agent and a stray click would race for the same page, and clicks
    would land at unpredictable moments.
    """
    await websocket.accept()
    queue = browser_session.subscribe(chat_id)

    async def send(payload: Dict[str, Any]) -> None:
        try:
            await websocket.send_json(payload)
        except Exception:
            pass  # viewer went away mid-send; the read loop will notice

    async def pump_frames():
        while True:
            message = await queue.get()
            await send(message)

    async def pump_input():
        # Read side: this is also how a client disconnect surfaces.
        while True:
            raw = await websocket.receive_text()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("kind")

            if kind == "ping":
                await send({"type": "pong"})
                continue

            # Control flips arrive over the socket too, so the button and any
            # keybinding share one code path.
            if kind == "control":
                active = bool(message.get("active"))
                browser_session.set_human_control(chat_id, active)
                if active:
                    await browser_session.focus_page()
                await send({"type": "control", **browser_session.control_state(chat_id)})
                continue

            if kind == "selection":
                # Copy from the page, for the viewer's own clipboard.
                await send({"type": "selection", "text": await browser_session.read_selection()})
                continue

            if not browser_session.is_human_controlled(chat_id):
                await send({"type": "input-rejected",
                            "reason": "Take control before interacting with the page."})
                continue

            result = await browser_session.dispatch_input(message)
            if result.get("error"):
                await send({"type": "input-error", "error": result["error"]})

    try:
        # Tell the viewer who is driving before the first frame, so the UI shows
        # the right affordance instead of guessing.
        await send({"type": "control", **browser_session.control_state(chat_id)})

        # Seed the viewer immediately. A screencast only emits on change, so a
        # static page would otherwise show an empty viewport until something moved.
        current = browser_session.get_current_url(chat_id)
        if current:
            await send({"type": "status", "url": current})
        try:
            seed = await browser_session.frame_jpeg()
            if seed:
                await send({"type": "frame", "data": seed})
        except Exception:
            pass  # the first real frame will arrive shortly

        pump = asyncio.create_task(pump_frames())
        reader = asyncio.create_task(pump_input())
        _done, pending = await asyncio.wait({pump, reader}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        browser_session.unsubscribe(chat_id, queue)
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/api/browser/snapshot")
async def browser_snapshot(chat_id: Optional[str] = Query(None)):
    """Returns the ref-annotated snapshot of the live page (no streaming)."""
    snapshot = await browser_session.observe(chat_id)
    if snapshot.get("error"):
        raise HTTPException(status_code=503, detail=snapshot["error"])
    return snapshot


@app.get("/api/browser/frame")
async def browser_frame():
    """Returns one base64 JPEG of the live page, for polling viewers."""
    data = await browser_session.frame_jpeg()
    if not data:
        raise HTTPException(status_code=503, detail="No frame available.")
    return {"data": data}


@app.post("/api/browser/action")
async def browser_action(chat_id: str, req: BrowserActionRequest):
    """Performs a single action on the live page (manual or scripted control)."""
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    return await browser_session.act(payload, chat_id)


@app.post("/api/browser/capture")
async def browser_capture(workspace_id: str):
    """Saves a PNG of the current live page into the given chat's workspace.

    Takes a workspace_id (not the chat id) because that is what the file APIs
    and the workspace panel are keyed on.
    """
    result = await browser_session.screenshot(chat_id=workspace_id)
    if result.get("error"):
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@app.post("/api/browser/task")
async def browser_task(req: BrowserTaskRequest):
    """Runs the autonomous browser agent over a goal, streaming progress as SSE."""
    from app.browser_agent import browser_agent

    async def event_generator():
        try:
            async for event in browser_agent.run(
                req.goal,
                chat_id=req.chat_id,
                model_name=req.model,
                max_steps=req.max_steps or 25,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/browser/takeover")
async def browser_takeover(chat_id: str, active: bool = Query(True)):
    """Hands the page to a human, or takes it back for the agent.

    The agent parks on a handoff rather than dying, so someone who steps in to
    type a password or solve a CAPTCHA can hand control back and let the run
    continue from wherever they left off.
    """
    state = browser_session.set_human_control(chat_id, active)
    if active:
        await browser_session.focus_page()
    return {"status": "ok", **state}


@app.post("/api/browser/answer")
async def browser_answer(chat_id: str, answer: str = Body("", embed=True)):
    """Delivers the user's reply to a parked agent.

    A 409 means nothing was waiting, which lets the UI tell a genuine no-op
    apart from a race.
    """
    if not browser_session.answer_handoff(chat_id, answer):
        raise HTTPException(status_code=409, detail="The agent is not waiting for an answer.")
    return {"status": "ok"}


@app.get("/api/browser/size")
async def browser_size():
    """The page's true pixel size, for mapping viewer clicks into page space."""
    return browser_session.viewport_size()


@app.post("/api/browser/stop")
async def browser_stop(chat_id: str):
    """Stops the running browser agent for this chat.

    Sets a stop flag the loop checks at its next step boundary, and releases the
    agent if it is parked on a question. It deliberately does *not* take control
    of the browser: a stopped run is over, whereas a take-over means the human
    is driving, and treating Stop as a take-over leaves the session parked for
    the next run.
    """
    browser_session.request_stop(chat_id)
    return {"status": "ok", **browser_session.control_state(chat_id)}


@app.post("/api/browser/cancel")
async def browser_cancel(chat_id: str):
    """Releases an agent parked on a question, without ending the run."""
    browser_session.cancel_handoff(chat_id)
    return {"status": "ok"}


@app.on_event("shutdown")
async def shutdown_browser():
    await browser_session.close()

# Static files for UI
static_dir = BASE_DIR / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
