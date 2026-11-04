"""Self-check: the whole class flow with a fake AI, on a throwaway database.

    .venv/bin/python test_app.py
"""
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/t.db"
os.environ["TUTOR_API_KEY"] = "fake"

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402

# --- the reply parser: unreadable means "ask the teacher" --------------------------
assert app.parse_tutor("IN_CLASS: yes\nANSWER: Because of old meanings.") == (True, "Because of old meanings.")
assert app.parse_tutor("in_class: NO\nanswer: I'll ask her.") == (False, "I'll ask her.")
assert app.parse_tutor("Sure! It's about etymology.") == (False, "Sure! It's about etymology.")

# --- fake AI: knows "parkway", not "subjunctive"; records what it was shown --------
seen = []


async def fake_llm(system, user):
    seen.append(user)
    if "profile" in system.lower():
        return "Uses road examples."
    if "subjunctive" in user.split("ASKS]")[-1]:
        return "IN_CLASS: no\nANSWER: I'm asking Giovanna."
    return "IN_CLASS: yes\nANSWER: Like Giovanna said, 'park' once meant a green area."

app._llm = fake_llm

with TestClient(app.app) as c:
    def person(name):
        return {"X-Person-Key": c.post("/api/people", json={"name": name}).json()["key"]}

    gio, siena, stranger = person("Giovanna"), person("Siena"), person("Eve")
    assert c.get("/api/classes", headers={}).status_code == 401

    cls = c.post("/api/classes", headers=gio, json={"title": "Parkway vs driveway",
                                                    "lang": "en-CA"}).json()
    assert c.post("/api/classes", headers=gio, json={"title": "x", "call_url": "javascript:1"}).status_code == 400
    cid = cls["id"]
    assert c.post(f"/api/classes/join/{cls['invite_token']}", headers=siena).json()["role"] == "student"
    assert c.get(f"/api/classes/{cid}", headers=stranger).status_code == 404
    assert "invite_token" not in c.get(f"/api/classes/{cid}", headers=siena).json()

    c.post(f"/api/classes/{cid}/lines", headers=gio, json={"text": "A parkway was a road through a park."})
    c.post(f"/api/classes/{cid}/lines", headers=siena, json={"text": "And driveway?"})
    lines = c.get(f"/api/classes/{cid}/lines", headers=siena).json()["lines"]
    assert [(l["name"], l["role"]) for l in lines] == [("Giovanna", "teacher"), ("Siena", "student")]
    assert c.get(f"/api/classes/{cid}/lines?after={lines[0]['id']}", headers=gio).json()["lines"][0]["text"] == "And driveway?"

    assert c.post(f"/api/classes/{cid}/end", headers=siena).status_code == 403
    assert c.post(f"/api/classes/{cid}/end", headers=gio).json()["status"] == "ended"
    assert c.post(f"/api/classes/{cid}/lines", headers=gio, json={"text": "late"}).status_code == 409

    # in the class: answered right away, from the transcript
    q1 = c.post(f"/api/classes/{cid}/ask", headers=siena, json={"question": "Why parkway?"}).json()
    assert q1["status"] == "answered" and "A parkway was a road through a park." in seen[-1]
    assert "Uses road examples." in seen[-1]  # the profiles built at the end reach the tutor
    assert c.post(f"/api/classes/{cid}/ask", headers=gio, json={"question": "x"}).status_code == 403

    # not in the class: goes to the teacher's inbox, her answer joins the pack
    q2 = c.post(f"/api/classes/{cid}/ask", headers=siena, json={"question": "What is the subjunctive?"}).json()
    assert q2["status"] == "waiting_teacher"
    inbox = c.get("/api/classes/inbox/waiting", headers=gio).json()
    assert [q["id"] for q in inbox] == [q2["id"]]
    assert c.get("/api/classes/inbox/waiting", headers=siena).json() == []
    assert c.post(f"/api/classes/questions/{q2['id']}/answer", headers=siena, json={"text": "x"}).status_code == 404
    c.post(f"/api/classes/questions/{q2['id']}/answer", headers=gio, json={"text": "It expresses wishes."})
    assert c.get("/api/classes/inbox/waiting", headers=gio).json() == []
    c.post(f"/api/classes/{cid}/ask", headers=siena, json={"question": "Wishes?"})
    assert "It expresses wishes." in seen[-1]

    # each student sees only their own questions; the teacher sees all
    assert len(c.get(f"/api/classes/{cid}/questions", headers=siena).json()) == 3
    assert len(c.get(f"/api/classes/{cid}/questions", headers=gio).json()) == 3
    assert c.get("/join/abc").status_code == 200  # the page handles invite links
    assert c.get("/api/tutor").json() == {"cloud": True, "model": app.TUTOR_MODEL}

    # --- the phone app: Llama answers on the phone, the server only stores -------
    pack = c.get(f"/api/classes/{cid}/pack", headers=siena).json()
    assert [l["text"] for l in pack["lines"]][0] == "A parkway was a road through a park."
    assert pack["teacher_answers"] == [{"question": "What is the subjunctive?", "answer": "It expresses wishes."}]
    assert pack["teacher_style"] == "Uses road examples." and pack["my_profile"] == "Uses road examples."
    assert c.get(f"/api/classes/{cid}/pack", headers=stranger).status_code == 404

    asked = len(seen)
    r = c.post(f"/api/classes/{cid}/questions", headers=siena,
               json={"question": "What is a gerund?", "ai_answer": "Sent to Giovanna.", "from_class": False}).json()
    assert r["status"] == "waiting_teacher" and len(seen) == asked  # no server AI involved
    assert [q["id"] for q in c.get("/api/classes/inbox/waiting", headers=gio).json()] == [r["id"]]
    assert c.post(f"/api/classes/{cid}/questions", headers=gio,
                  json={"question": "x", "ai_answer": "y", "from_class": True}).status_code == 403

    assert c.put("/api/profiles/teacher", headers=gio, json={"text": "Draws maps."}).status_code == 200
    assert c.get(f"/api/classes/{cid}/pack", headers=siena).json()["teacher_style"] == "Draws maps."
    assert c.put("/api/profiles/admin", headers=gio, json={"text": "x"}).status_code == 400

# --- meetings, shared memory, the assistant and the MCP plugin ---------------------
import json as _json  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402

import memory  # noqa: E402

pages = {}


async def fake_memory(tool, args):
    """ai-memory in a dict: same errors for a project with no pages yet."""
    proj = args["project"]
    if tool == "memory_write_page":
        pages[(proj, args["path"])] = args["body"]
        return {"path": args["path"]}
    if not any(pr == proj for pr, _ in pages):
        raise memory.MemoryError(f"project '{proj}' not found")
    if tool == "memory_query":
        words = args["query"].lower().split()
        return {"hits": [{"path": pa, "title": pa, "rank": -1}
                         for (pr, pa), b in pages.items() if pr == proj and any(w in b.lower() for w in words)]}
    if tool == "memory_read_page":
        return {"title": args["path"], "body": pages[(proj, args["path"])]}


memory.URL, memory._call = "http://fake", fake_memory


async def recap_llm(system, user):
    if "recap" in system.lower():
        return "## Summary\nBudget talk.\n## Decisions\nShip Friday.\n## Action items\n- Ana: send deck (due: Friday)\n## Open questions\nNone."
    return "A profile."

app._llm = recap_llm

with TestClient(app.app) as c:
    def person(name):
        return {"X-Person-Key": c.post("/api/people", json={"name": name}).json()["key"]}

    host, ana, eve = person("Bob"), person("Ana"), person("Eve")
    assert c.post("/api/classes", headers=host, json={"title": "x", "kind": "party"}).status_code == 400
    mt = c.post("/api/classes", headers=host, json={"title": "Q3 budget", "kind": "meeting"}).json()
    assert mt["kind"] == "meeting" and mt["recap"] is None
    c.post(f"/api/classes/join/{mt['invite_token']}", headers=ana)
    c.post(f"/api/classes/{mt['id']}/lines", headers=host, json={"text": "We ship the budget deck Friday."})
    c.post(f"/api/classes/{mt['id']}/lines", headers=ana, json={"text": "I will send the deck."})
    assert c.put(f"/api/classes/{mt['id']}/recap", headers=host, json={"text": "x"}).status_code == 409
    c.post(f"/api/classes/{mt['id']}/end", headers=host)

    # the server AI wrote the recap; transcript and recap are in the shared memory
    got = c.get(f"/api/classes/{mt['id']}", headers=ana).json()
    assert "Ship Friday." in got["recap"]
    proj = memory.session_project(mt["id"])
    assert "Bob (host)" in pages[(proj, "transcript.md")] and "Ana: send deck" in pages[(proj, "recap.md")]

    # a phone running Llama sends its own recap; only the host may
    assert c.put(f"/api/classes/{mt['id']}/recap", headers=ana, json={"text": "x"}).status_code == 403
    assert c.put(f"/api/classes/{mt['id']}/recap", headers=host,
                 json={"text": "## Summary\nDeck Friday.\n## Action items\n- Ana: deck"}).json()["recap"].count("\n") == 3

    # memory search sees only your own classes and meetings
    assert c.get("/api/memory/search?q=deck", headers=ana).json()["hits"]
    assert c.get("/api/memory/search?q=deck", headers=eve).json()["hits"] == []
    assert c.post("/api/memory/notes", headers=eve, json={"text": "Eve's deck idea"}).status_code == 201
    assert not any("Eve" in h["text"] for h in c.get("/api/memory/search?q=deck", headers=ana).json()["hits"])

    # the assistant: Muse searches memory, then proposes a calendar event
    calls = []

    async def fake_chat(messages, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return NS(content="", tool_calls=[
                NS(id="1", function=NS(name="search_memory", arguments=_json.dumps({"query": "deck"}))),
                NS(id="2", function=NS(name="add_calendar_event", arguments=_json.dumps(
                    {"title": "Send deck", "start": "2026-11-07T09:00:00-08:00", "duration_minutes": 9999}))),
                NS(id="3", function=NS(name="add_calendar_event", arguments='{"title": "bad", "start": "Friday"}'))])
        return NS(content="Ana sends the deck Friday; I proposed a calendar event.", tool_calls=None)

    app._chat = fake_chat
    r = c.post("/api/assistant", headers=ana, json={
        "messages": [{"role": "user", "content": "When is the deck due? Remind me."}],
        "now": "2026-11-03T20:00:00-08:00"}).json()
    assert r["reply"].startswith("Ana sends the deck")
    assert r["actions"] == [{"type": "add_calendar_event", "title": "Send deck",
                             "start": "2026-11-07T09:00:00-08:00", "duration_minutes": 30, "description": ""}]
    tool_msgs = [m for m in calls[1] if m["role"] == "tool"]
    assert "Deck Friday." in tool_msgs[0]["content"] and "unusable" in tool_msgs[2]["content"]
    assert calls[1][-4]["tool_calls"][0]["function"]["name"] == "search_memory"  # full assistant message sent back
    assert c.post("/api/assistant", headers=ana, json={"messages": [
        {"role": "assistant", "content": "hi"}]}).status_code == 400

    # the MCP plugin: the person's key is the bearer
    ana_key = ana["X-Person-Key"]
    bearer = {"Authorization": f"Bearer {ana_key}", "Accept": "application/json, text/event-stream"}
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert c.post("/mcp", headers={**bearer, "Origin": "https://evil.example"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 403
    assert c.get("/mcp", headers=bearer).status_code == 405
    init = c.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                "params": {"protocolVersion": "2025-06-18"}}).json()
    assert init["result"]["capabilities"] == {"tools": {}}
    assert c.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    names = [t["name"] for t in c.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]]
    assert names == ["search_memory", "save_note"]
    hit = c.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                               "params": {"name": "search_memory", "arguments": {"query": "deck"}}}).json()
    assert "Deck Friday." in hit["result"]["content"][0]["text"] and hit["result"]["isError"] is False
    assert c.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 4, "method": "nope"}).json()["error"]["code"] == -32601

memory.URL = ""

# --- a slow real AI: profiles being written must not lock out a student asking ---
import asyncio  # noqa: E402

import httpx  # noqa: E402


async def slow_llm(system, user):
    # As in production: the teacher's profile is saved at 8 s, the student's
    # call runs to 16 s, and the tutor's reply is saved at 9 s, in between,
    # for longer than sqlite's 5 s default lock wait.
    if "profile" in system.lower():
        await asyncio.sleep(8)
        return "Uses road examples."
    await asyncio.sleep(9)
    return "IN_CLASS: yes\nANSWER: ok"

app._llm = slow_llm


async def overlap():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://t") as ac:
        r, _ = await asyncio.gather(
            ac.post(f"/api/classes/{cid}/ask", headers=siena, json={"question": "Why parkway?"}),
            app._update_profiles(cid))
        return r

assert asyncio.run(overlap()).status_code == 200

print("class tutor: ok")
