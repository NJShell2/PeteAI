import asyncio
from app.agent import pete_agent
from app.storage import storage

async def test_moon_question():
    chat = storage.create_chat("Moon Test Chat")
    print("Testing question: 'What year did we go to the moon?'")
    
    events = []
    async for event in pete_agent.run(chat.id, "What year did we go to the moon?"):
        events.append(event)
        if event.get("type") == "tool_call":
            print(f"[TOOL CALLED] {event.get('name')}: {event.get('arguments')}")
        elif event.get("type") == "tool_result":
            res = str(event.get("result"))[:80]
            print(f"[TOOL RESULT] {res}...")
        elif event.get("type") == "done":
            ans = event.get("content", "")[:120].encode('ascii', 'ignore').decode('ascii')
            print(f"[FINAL ANSWER] {ans}...")

    storage.delete_chat(chat.id)
    print("Test finished successfully!")

if __name__ == "__main__":
    asyncio.run(test_moon_question())
