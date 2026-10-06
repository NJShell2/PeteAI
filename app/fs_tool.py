"""Local filesystem access for Pete AI.

Read access is machine-wide: Pete can inspect any file the current OS user can
read, which is what makes it useful for analysing your own notes, code and
downloads. Write access is deliberately narrower -- it is confined to an
allow-list of roots (the chat workspace plus any folders the user adds in
Settings), so a confused or prompt-injected model cannot overwrite arbitrary
files elsewhere on the machine.

Every public function returns a plain dict so the agent can serialise the
result straight into the conversation, and reports problems as a
``{"error": ...}`` payload rather than raising into the tool loop.
"""
import os
import fnmatch
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import WORKSPACES_DIR, load_settings

# Guards that keep a single tool result from flooding the model context.
MAX_READ_BYTES = 200_000
MAX_LIST_ENTRIES = 400
MAX_SEARCH_RESULTS = 100

# Directories that are enormous, generated, or private; never worth walking.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", "dist", "build", ".gradle",
    "AppData", "System Volume Information", "$RECYCLE.BIN", ".cache",
}

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".py", ".json", ".csv", ".tsv", ".js", ".jsx",
    ".ts", ".tsx", ".html", ".htm", ".css", ".yaml", ".yml", ".xml", ".sh",
    ".bash", ".ps1", ".bat", ".c", ".h", ".cpp", ".hpp", ".java", ".r", ".sql",
    ".env", ".log", ".ini", ".cfg", ".toml", ".rst", ".tex", ".ipynb",
}


def _ok(**payload: Any) -> Dict[str, Any]:
    return payload


def _err(message: str) -> Dict[str, Any]:
    return {"error": message}


def format_size(num_bytes: int) -> str:
    """Renders a byte count the way a file browser shows it (1.2 MB)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _expand(path_str: str) -> Path:
    """Expands ~, env vars and relative paths into an absolute normalized path."""
    raw = (path_str or "").strip().strip('"')
    if not raw:
        raise ValueError("A path is required.")
    raw = os.path.expandvars(raw)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def _real(path: Path) -> Path:
    """Fully resolves a path so symlink and '..' escapes cannot sneak through."""
    try:
        return path.resolve()
    except Exception:
        return path


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(child), str(parent)]) == str(parent)
    except ValueError:
        return False  # different Windows drives are never contained


def allowed_write_roots(workspace_id: Optional[str] = None) -> List[Path]:
    """Write-enabled roots: the chat workspace plus the user's configured folders."""
    roots: List[Path] = []
    if workspace_id:
        roots.append(_real(WORKSPACES_DIR / workspace_id))
    for raw in (load_settings().local_write_roots or []):
        try:
            roots.append(_real(_expand(raw)))
        except Exception:
            continue
    seen, unique = set(), []
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _require_writable(path: Path, workspace_id: Optional[str]) -> Path:
    """Returns `path` if it is inside an allowed write root, else raises."""
    roots = allowed_write_roots(workspace_id)
    if not roots:
        raise PermissionError(
            "Writing outside the chat workspace is disabled because no write-enabled "
            "folders are configured. Add folders in Settings > Local file access."
        )
    # Check the target and its parent so brand-new files are covered too.
    for candidate in (_real(path), _real(path.parent)):
        for root in roots:
            if _is_within(candidate, root):
                return path
    listed = "\n".join(f"  - {r}" for r in roots) or "  (none configured)"
    raise PermissionError(
        f"Write blocked: {path} is outside the write-enabled folders:\n{listed}\n"
        "Read access is unrestricted; only writes are limited."
    )


def looks_binary(path: Path) -> bool:
    """True when a file holds bytes that are not decodable text."""
    if path.suffix.lower() in {".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif",
                               ".webp", ".ico", ".exe", ".dll", ".docx", ".xlsx",
                               ".pptx", ".mp3", ".mp4", ".woff", ".woff2", ".ttf"}:
        return True
    try:
        with open(path, "rb") as handle:
            return b"\x00" in handle.read(4096)
    except Exception:
        return False




def _entry_info(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
        size, modified = stat.st_size, stat.st_mtime
    except Exception:
        size, modified = 0, 0.0
    return {
        "name": path.name,
        "path": str(path),
        "type": "directory" if path.is_dir() else "file",
        "size": size,
        "size_human": format_size(size),
        "modified": modified,
    }


def list_directory(path: str, recursive: bool = False, pattern: str = "",
                   max_entries: int = MAX_LIST_ENTRIES) -> Dict[str, Any]:
    """Lists a directory anywhere on the machine (read-only, unrestricted)."""
    try:
        root = _expand(path)
    except ValueError as exc:
        return _err(str(exc))
    if not root.exists():
        return _err(f"Path does not exist: {root}")
    if not root.is_dir():
        return _err(f"Not a directory: {root}. Use read_file to read a single file.")

    try:
        walker = root.rglob("*") if recursive else root.iterdir()
    except Exception as exc:
        return _err(f"Could not list {root}: {exc}")

    entries: List[Dict[str, Any]] = []
    truncated = False
    for item in walker:
        if len(entries) >= max_entries:
            truncated = True
            break
        if recursive and any(part in SKIP_DIRS for part in item.parts):
            continue
        if pattern and not fnmatch.fnmatch(item.name.lower(), pattern.lower()):
            continue
        entries.append(_entry_info(item))

    directories = sorted(e["name"] for e in entries if e["type"] == "directory")
    files = sorted(
        (e for e in entries if e["type"] == "file"),
        key=lambda e: e["name"].lower(),
    )
    summary = f"{root}  ({len(directories)} dir(s), {len(files)} file(s)"
    if truncated:
        summary += f", truncated at {max_entries}"
    summary += ")"

    return _ok(
        path=str(root),
        recursive=bool(recursive),
        directories=directories,
        files=files,
        summary=summary,
        truncated=truncated,
    )


def read_file(path: str, start_line: int = 1, end_line: int = 0,
              max_bytes: int = MAX_READ_BYTES) -> Dict[str, Any]:
    """Reads any text file on the machine. Unrestricted: this is a read-only tool."""
    try:
        target = _expand(path)
    except ValueError as exc:
        return _err(str(exc))
    if not target.exists():
        return _err(f"File does not exist: {target}")
    if target.is_dir():
        return _err(f"{target} is a directory. Use list_directory to browse it.")
    if looks_binary(target):
        return _err(
            f"{target.name} looks like a binary file ({format_size(target.stat().st_size)}); "
            "it has no readable text. Try a different file."
        )

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(max_bytes + 1)
    except Exception as exc:
        return _err(f"Could not read {target}: {exc}")

    truncated = len(text) > max_bytes
    if truncated:
        text = text[:max_bytes]

    line_count = text.count("\n") + 1
    if start_line < 1:
        start_line = 1
    if end_line and end_line < start_line:
        return _err(f"end_line ({end_line}) must be >= start_line ({start_line}).")

    lines = text.splitlines()
    selected = lines[start_line - 1:(end_line or len(lines))]
    body = "\n".join(selected)

    return _ok(
        path=str(target),
        content=body,
        lines_returned=len(selected),
        total_lines=line_count,
        size_human=format_size(target.stat().st_size),
        truncated=truncated,
        note=("Truncated at max_bytes." if truncated else None),
    )


def search_files(root: str, query: str, file_pattern: str = "*",
                 search_content: bool = True,
                 max_results: int = MAX_SEARCH_RESULTS) -> Dict[str, Any]:
    """Finds files by name, or greps their contents, under any directory."""
    if not (query or "").strip():
        return _err("A search query is required.")
    try:
        base = _expand(root)
    except ValueError as exc:
        return _err(str(exc))
    if not base.exists():
        return _err(f"Path does not exist: {base}")
    if not base.is_dir():
        base = base.parent

    needle = query.lower()
    matches: List[Dict[str, Any]] = []
    content_hits: List[Dict[str, Any]] = []
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            if not fnmatch.fnmatch(filename.lower(), file_pattern.lower()):
                continue
            full = Path(dirpath) / filename
            scanned += 1
            if needle in filename.lower():
                matches.append(_entry_info(full))
                if len(matches) >= max_results:
                    return _ok(root=str(base), query=query, mode="filename",
                               matches=matches, content_matches=[],
                               files_scanned=scanned)
                continue
            if not search_content or looks_binary(full):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    for line_no, line in enumerate(handle, start=1):
                        if needle in line.lower():
                            content_hits.append({"path": str(full), "line": line_no,
                                                 "text": line.strip()[:200]})
                            if len(content_hits) >= max_results:
                                return _ok(root=str(base), query=query, mode="content",
                                           matches=matches, content_matches=content_hits,
                                           files_scanned=scanned)
                            break  # one hit per file keeps results readable
            except Exception:
                continue

    return _ok(
        root=str(base),
        query=query,
        mode="content" if search_content else "filename",
        matches=matches,
        content_matches=content_hits,
        files_scanned=scanned,
        summary=(f"{len(matches)} filename match(es), {len(content_hits)} content match(es) "
                 f"across {scanned} file(s)."),
    )


# ── Write-capable operations (allow-list enforced) ───────────────────────────────

def write_file(path: str, content: str, workspace_id: Optional[str] = None,
               append: bool = False) -> Dict[str, Any]:
    """Creates or overwrites a file, but only inside an allowed write root."""
    try:
        target = _expand(path)
        _require_writable(target, workspace_id)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a" if append else "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception as exc:
        return _err(f"Could not write {target}: {exc}")

    verb = "Appended to" if append else "Wrote"
    return _ok(
        path=str(target),
        action="append" if append else "write",
        characters=len(content),
        size_human=format_size(target.stat().st_size),
        message=f"{verb} {target.name} ({len(content)} characters).",
    )


def make_directory(path: str, workspace_id: Optional[str] = None) -> Dict[str, Any]:
    """Creates a directory, but only inside an allowed write root."""
    try:
        target = _expand(path)
        _require_writable(target, workspace_id)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return _err(f"Could not create {target}: {exc}")
    return _ok(path=str(target), message=f"Created directory {target}.")


def delete_path(path: str, workspace_id: Optional[str] = None,
                recursive: bool = False) -> Dict[str, Any]:
    """Deletes a file or directory, but only inside an allowed write root."""
    try:
        target = _expand(path)
        _require_writable(target, workspace_id)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))
    if not target.exists():
        return _err(f"Path does not exist: {target}")
    try:
        if target.is_dir():
            if not recursive and any(target.iterdir()):
                return _err(f"{target.name} is not empty. Re-run with recursive=true to "
                            "delete it and everything inside it.")
            shutil.rmtree(target)
        else:
            target.unlink()
    except Exception as exc:
        return _err(f"Could not delete {target}: {exc}")
    return _ok(path=str(target), message=f"Deleted {target}.")


def move_path(source: str, destination: str, workspace_id: Optional[str] = None,
              overwrite: bool = False) -> Dict[str, Any]:
    """Moves or renames a path; both ends must be inside an allowed write root."""
    try:
        src, dst = _expand(source), _expand(destination)
        _require_writable(src, workspace_id)
        _require_writable(dst, workspace_id)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))
    if not src.exists():
        return _err(f"Source does not exist: {src}")
    if dst.exists() and not overwrite:
        return _err(f"{dst} already exists. Re-run with overwrite=true to replace it.")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except Exception as exc:
        return _err(f"Could not move {src} to {dst}: {exc}")
    return _ok(source=str(src), destination=str(dst),
               message=f"Moved {src.name} to {dst}.")
