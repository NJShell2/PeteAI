import asyncio
import sys
from app.config import load_settings
from app.storage import storage
from app.browser_tool import browser_tool
from app.api import app

async def test_all():
    print("Testing Configuration...")
    settings = load_settings()
    assert settings.purdue_api_url == "https://genai.rcac.purdue.edu/api/v1"
    print("[OK] Config OK")

    print("Testing Storage Manager...")
    root_chat = storage.create_chat("Test Root Chat")
    print(f"[OK] Created root chat: {root_chat.id}")

    subchat = storage.create_chat("Test Subchat", parent_id=root_chat.id)
    print(f"[OK] Created subchat: {subchat.id} (inherited workspace: {subchat.workspace_id})")
    assert subchat.workspace_id == root_chat.workspace_id

    # Test file write & read
    write_res = storage.write_workspace_text_file(root_chat.workspace_id, "hello.txt", "Boiler Up!")
    print(f"[OK] File write: {write_res}")

    content = storage.read_workspace_file(subchat.workspace_id, "hello.txt")
    print(f"[OK] Subchat read inherited file: {content}")
    assert content == "Boiler Up!"

    # Clean up
    storage.delete_chat(root_chat.id)
    print("[OK] Chat and subchat cleanup OK")

    print("Testing Web Search fallback...")
    results = await browser_tool.search_web("Purdue University", max_results=2)
    print(f"[OK] Web search returned {len(results)} items: {[r.get('title') for r in results]}")

    print("\nALL BACKEND CHECKS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(test_all())
