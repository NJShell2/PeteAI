import uvicorn
import webbrowser
import threading
import time
import sys
import os

# Early debug - write to a file immediately so we can see if Python runs
try:
    debug_log = os.path.join(os.environ.get('LOCALAPPDATA', '.'), 'PeteAI', 'debug_launch.log')
    os.makedirs(os.path.dirname(debug_log), exist_ok=True)
    with open(debug_log, 'w', encoding='utf-8') as f:
        f.write(f"main.py started\n")
        f.write(f"sys.executable: {sys.executable}\n")
        f.write(f"sys.frozen: {getattr(sys, 'frozen', False)}\n")
        f.write(f"sys.argv: {sys.argv}\n")
        f.write(f"cwd: {os.getcwd()}\n")
        f.write(f"BASE_DIR import...\n")
except Exception as e:
    pass

from app.config import BASE_DIR
from app.console import safe_print

try:
    with open(debug_log, 'a', encoding='utf-8') as f:
        f.write(f"BASE_DIR: {BASE_DIR}\n")
        f.write(f"Imports done\n")
except Exception as e:
    pass


def open_browser(url: str):
    # Wait until the server actually answers before opening the tab. A fixed
    # sleep was a race: on slower machines uvicorn was still starting when the
    # browser loaded, so the first load failed and the user had to refresh.
    import urllib.request
    for _ in range(60):  # up to ~30 seconds
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 400:
                    break
        except Exception:
            time.sleep(0.5)
    try:
        webbrowser.open(url)
    except Exception as e:
        safe_print(f"Could not open browser automatically: {e}")


if __name__ == "__main__":
    host = "127.0.0.1"
    port = 8000
    app_url = f"http://{host}:{port}"

    safe_print("=" * 60)
    safe_print(" Purdue Pete AI (GenAI Studio Agent)")
    safe_print(" Pete Solo (Subchats) | Pete Squad | Local Workspace & Browser")
    safe_print("=" * 60)
    safe_print(f" Web UI running at: {app_url}")
    safe_print(" Press Ctrl+C to stop.")
    safe_print("=" * 60)

    # Launch browser in a background thread
    threading.Thread(target=open_browser, args=(app_url,), daemon=True).start()

    # Start FastAPI server
    uvicorn.run("app.api:app", host=host, port=port, reload=False, log_level="info")
