import os
import json
import uuid
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

from app.config import CHATS_DIR, WORKSPACES_DIR
from app.console import safe_print

# See save_chat(): the network share intermittently denies a rewrite of an
# existing file. A few short retries make saving reliable; see the comment there.
_SAVE_RETRIES = 6
_SAVE_RETRY_BACKOFF = 0.05

class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: str # "user", "assistant", "system", "tool"
    content: str = ""
    timestamp: float = Field(default_factory=time.time)
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    subchat_id: Optional[str] = None # If this message triggered or links to a subchat
    metadata: Dict[str, Any] = Field(default_factory=dict)

class Chat(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = "New Chat"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    parent_id: Optional[str] = None  # None for main chat; chat_id for subchat
    workspace_id: str = ""           # Links to workspace folder (subchats inherit parent workspace)
    messages: List[Message] = Field(default_factory=list)
    model: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

class WorkspaceFileInfo(BaseModel):
    name: str
    size: int
    modified: float
    is_text: bool
    path: str
    extension: str

class StorageManager:
    def __init__(self):
        self.chats_dir = CHATS_DIR
        self.workspaces_dir = WORKSPACES_DIR

    # --- Chat & Subchat Operations ---

    def create_chat(self, title: str = "New Chat", parent_id: Optional[str] = None, workspace_id: Optional[str] = None, model: Optional[str] = None) -> Chat:
        chat_id = str(uuid.uuid4())
        
        # If it's a subchat and no workspace_id provided, inherit parent workspace
        if parent_id and not workspace_id:
            parent = self.get_chat(parent_id)
            if parent:
                workspace_id = parent.workspace_id
                
        if not workspace_id:
            workspace_id = chat_id

        # Ensure workspace directory exists
        ws_path = self.get_workspace_path(workspace_id)
        ws_path.mkdir(parents=True, exist_ok=True)

        chat = Chat(
            id=chat_id,
            title=title,
            parent_id=parent_id,
            workspace_id=workspace_id,
            model=model,
            messages=[]
        )
        self.save_chat(chat)
        return chat

    def get_chat(self, chat_id: str) -> Optional[Chat]:
        chat_file = self.chats_dir / f"{chat_id}.json"
        if not chat_file.exists():
            return None
        try:
            with open(chat_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return Chat(**data)
        except Exception as e:
            safe_print(f"Error loading chat {chat_id}: {e}")
            return None

    def save_chat(self, chat: Chat):
        chat.updated_at = time.time()
        chat_file = self.chats_dir / f"{chat.id}.json"
        payload = json.dumps(chat.model_dump(), indent=2, ensure_ascii=False)
        last_error = None
        for attempt in range(_SAVE_RETRIES):
            try:
                with open(chat_file, "w", encoding="utf-8") as f:
                    f.write(payload)
                return
            except PermissionError as e:
                # data/ lives on a UNC network share. Rewriting a file that some
                # other handle (the running server, a search indexer, the SMB
                # redirector) has open intermittently fails with EACCES even
                # though the file is plainly ours and writable. Creating a new
                # file in the same directory never fails this way, which is why
                # only *updates* were flaky. The window is sub-second, so a short
                # backoff clears it. Without this, saving a message throws and
                # the turn is lost.
                last_error = e
                time.sleep(_SAVE_RETRY_BACKOFF * (attempt + 1))
        raise last_error

    def delete_chat(self, chat_id: str) -> bool:
        # Also delete all children subchats
        subchats = self.get_subchats(chat_id)
        for sub in subchats:
            self.delete_chat(sub.id)
            
        chat_file = self.chats_dir / f"{chat_id}.json"
        if chat_file.exists():
            chat_file.unlink()
            return True
        return False

    def list_all_chats(self) -> List[Chat]:
        chats = []
        for file in self.chats_dir.glob("*.json"):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    chats.append(Chat(**data))
            except Exception:
                continue
        # Sort by updated_at descending
        chats.sort(key=lambda c: c.updated_at, reverse=True)
        return chats

    def get_chat_tree(self) -> List[Dict[str, Any]]:
        """Returns root chats with their nested subchats in a tree structure."""
        all_chats = self.list_all_chats()
        chat_map = {c.id: c.model_dump() for c in all_chats}
        
        # Add subchats array to each
        for cid in chat_map:
            chat_map[cid]["subchats"] = []

        root_chats = []
        for c in all_chats:
            if c.parent_id and c.parent_id in chat_map:
                chat_map[c.parent_id]["subchats"].append(chat_map[c.id])
            else:
                root_chats.append(chat_map[c.id])

        # Sort subchats by created_at ascending
        for c in chat_map.values():
            c["subchats"].sort(key=lambda x: x["created_at"])

        return root_chats

    def get_subchats(self, parent_id: str) -> List[Chat]:
        all_chats = self.list_all_chats()
        return [c for c in all_chats if c.parent_id == parent_id]

    def add_message(self, chat_id: str, message: Message) -> Optional[Chat]:
        chat = self.get_chat(chat_id)
        if not chat:
            return None
        chat.messages.append(message)
        # Update chat title if it's the first user message and title is default
        if len(chat.messages) == 1 and message.role == "user" and chat.title == "New Chat":
            clean_title = message.content.strip().replace("\n", " ")[:40]
            if clean_title:
                chat.title = clean_title
        self.save_chat(chat)
        return chat

    def get_breadcrumbs(self, chat_id: str) -> List[Dict[str, str]]:
        """Returns the ancestor path list from root chat down to current chat."""
        crumbs = []
        curr = self.get_chat(chat_id)
        visited = set()
        while curr and curr.id not in visited:
            visited.add(curr.id)
            crumbs.insert(0, {"id": curr.id, "title": curr.title, "is_subchat": curr.parent_id is not None})
            if curr.parent_id:
                curr = self.get_chat(curr.parent_id)
            else:
                break
        return crumbs

    # --- File Workspace Operations ---

    def get_workspace_path(self, workspace_id: str) -> Path:
        path = self.workspaces_dir / workspace_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def list_files(self, workspace_id: str) -> List[WorkspaceFileInfo]:
        ws_path = self.get_workspace_path(workspace_id)
        files = []
        for p in ws_path.iterdir():
            if p.is_file():
                ext = p.suffix.lower()
                is_text = ext in [".txt", ".md", ".py", ".json", ".csv", ".js", ".html", ".css", ".yaml", ".yml", ".xml", ".sh", ".c", ".cpp", ".java", ".r", ".sql", ".env", ".log"]
                files.append(WorkspaceFileInfo(
                    name=p.name,
                    size=p.stat().st_size,
                    modified=p.stat().st_mtime,
                    is_text=is_text,
                    path=str(p),
                    extension=ext
                ))
        files.sort(key=lambda x: x.name.lower())
        return files

    def save_workspace_file(self, workspace_id: str, filename: str, content: bytes) -> WorkspaceFileInfo:
        # Sanitize filename
        safe_name = Path(filename).name
        ws_path = self.get_workspace_path(workspace_id)
        target = ws_path / safe_name
        with open(target, "wb") as f:
            f.write(content)
        ext = target.suffix.lower()
        is_text = ext in [".txt", ".md", ".py", ".json", ".csv", ".js", ".html", ".css", ".yaml", ".yml", ".xml", ".sh", ".c", ".cpp", ".java", ".r", ".sql", ".env", ".log"]
        return WorkspaceFileInfo(
            name=safe_name,
            size=target.stat().st_size,
            modified=target.stat().st_mtime,
            is_text=is_text,
            path=str(target),
            extension=ext
        )

    def read_workspace_file(self, workspace_id: str, filename: str) -> str:
        safe_name = Path(filename).name
        ws_path = self.get_workspace_path(workspace_id)
        target = ws_path / safe_name
        if not target.exists():
            return f"Error: File '{safe_name}' does not exist in the workspace."
        
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            return f"Error reading file '{safe_name}': {str(e)}"

    def write_workspace_text_file(self, workspace_id: str, filename: str, content: str) -> str:
        safe_name = Path(filename).name
        ws_path = self.get_workspace_path(workspace_id)
        target = ws_path / safe_name
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully saved '{safe_name}' ({len(content)} characters)."
        except Exception as e:
            return f"Error writing file '{safe_name}': {str(e)}"

    def delete_workspace_file(self, workspace_id: str, filename: str) -> bool:
        safe_name = Path(filename).name
        ws_path = self.get_workspace_path(workspace_id)
        target = ws_path / safe_name
        if target.exists():
            target.unlink()
            return True
        return False

storage = StorageManager()
