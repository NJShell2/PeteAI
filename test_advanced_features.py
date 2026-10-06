import asyncio
from app.config import load_settings
from app.storage import storage, Message
from app.agent import pete_agent

async def test_advanced():
    print("Testing Breadcrumbs & Subchat...")
    parent = storage.create_chat("Main Research Topic")
    sub1 = storage.create_chat("Deep Investigation 1", parent_id=parent.id)
    sub2 = storage.create_chat("Sub-topic 1.1", parent_id=sub1.id)

    crumbs = storage.get_breadcrumbs(sub2.id)
    print(f"[OK] Breadcrumbs length: {len(crumbs)} (expected 3)")
    assert len(crumbs) == 3
    assert crumbs[0]["id"] == parent.id
    assert crumbs[1]["id"] == sub1.id
    assert crumbs[2]["id"] == sub2.id

    print("Testing In-Browser Workspace File Creation & Editing...")
    ws_id = parent.workspace_id
    storage.write_workspace_text_file(ws_id, "notes.md", "# Original Title\nInitial notes.")
    content1 = storage.read_workspace_file(ws_id, "notes.md")
    assert "Original Title" in content1

    # Edit file
    storage.write_workspace_text_file(ws_id, "notes.md", "# Updated Title\nEdited in-browser.")
    content2 = storage.read_workspace_file(ws_id, "notes.md")
    assert "Updated Title" in content2
    print("[OK] File edit test passed")

    print("Testing Subchat Synthesis into Parent Chat...")
    # Add messages to sub1
    storage.add_message(sub1.id, Message(role="user", content="What are the key advantages of Playwright for headless research?"))
    storage.add_message(sub1.id, Message(role="assistant", content="Playwright provides fast chromium headless automation, full screenshot support, and async event handling."))

    synth_msg = await pete_agent.synthesize_subchat(sub1.id)
    assert synth_msg is not None
    clean_sample = synth_msg.content[:100].encode('ascii', 'ignore').decode('ascii')
    print(f"[OK] Subchat synthesized into parent message:\n{clean_sample}...")

    # Verify parent chat has the new synthesis message
    updated_parent = storage.get_chat(parent.id)
    assert len(updated_parent.messages) == 1
    assert "Subchat Synthesis" in updated_parent.messages[0].content
    print("[OK] Parent chat verified received synthesis message")

    # Cleanup
    storage.delete_chat(parent.id)
    print("[OK] Cleanup completed successfully")

    print("\nALL ADVANCED TESTS PASSED PERFECTLY!")

if __name__ == "__main__":
    asyncio.run(test_advanced())
