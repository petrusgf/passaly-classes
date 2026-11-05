"""Self-check: the whole class flow with a fake AI, on a throwaway database.

    .venv/bin/python test_app.py
"""
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/t.db"
os.environ["TUTOR_API_KEY"] = "fake"
os.environ["FILES_DIR"] = tempfile.mkdtemp()

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
    assert c.get("/api/tutor").json() == {"cloud": True, "model": app.TUTOR_MODEL, "google": False}

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
        if (proj, args["path"]) not in pages:
            raise memory.MemoryError("page not found")
        return {"title": args["path"], "body": pages[(proj, args["path"])]}
    if tool == "memory_recent":
        return {"hits": [{"path": pa, "title": pa, "snippet": b[:80]} for (pr, pa), b in pages.items() if pr == proj]}


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
    # no keyword match: the newest recaps instead, still only your own
    fallback = c.get("/api/memory/search?q=deliverables", headers=ana).json()["hits"]
    assert [h["path"] for h in fallback] == ["recap.md"]
    assert c.get("/api/memory/search?q=deliverables", headers=eve).json()["hits"] == []
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

    # the MCP plugin: an MCP token is the bearer, never the login key
    def mcp_token(who, write=False):
        return c.post("/api/mcp/tokens", headers=who, json={"name": "Claude", "can_write": write}).json()

    made = mcp_token(ana, write=True)
    ana_key = made["token"]
    assert ana_key.startswith("pcm_") and made["can_write"] is True
    assert all("token" not in t for t in c.get("/api/mcp/tokens", headers=ana).json())  # shown once
    import sqlite3  # noqa: E402
    db_file = os.environ["DATABASE_URL"].split("///", 1)[1]
    assert sqlite3.connect(db_file).execute("select count(*) from mcp_token where token_hash = ?",
                                            (ana_key,)).fetchone()[0] == 0  # only the hash is stored
    bearer = {"Authorization": f"Bearer {ana_key}", "Accept": "application/json, text/event-stream"}
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    login_key = {"Authorization": f"Bearer {ana['X-Person-Key']}"}
    assert c.post("/mcp", headers=login_key, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert c.post("/mcp", headers={**bearer, "Origin": "https://evil.example"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 403
    assert c.get("/mcp", headers=bearer).status_code == 405
    init = c.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                "params": {"protocolVersion": "2025-06-18"}}).json()
    assert init["result"]["capabilities"] == {"tools": {}, "resources": {}}
    assert c.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    names = [t["name"] for t in c.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]]
    assert names[:2] == ["search_memory", "save_note"]
    hit = c.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                               "params": {"name": "search_memory", "arguments": {"query": "deck"}}}).json()
    assert "Deck Friday." in hit["result"]["content"][0]["text"] and hit["result"]["isError"] is False
    assert c.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 4, "method": "nope"}).json()["error"]["code"] == -32601


# --- documents in a session, and the MCP server as a file host ----------------------


def tiny_pdf(text: str) -> bytes:
    """A one-page PDF with `text`, built by hand (no PDF writer installed)."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)


with TestClient(app.app) as c:
    mid = mt["id"]
    up = c.post(f"/api/classes/{mid}/files?name=Launch plan.pdf", headers=ana,
                content=tiny_pdf("Launch checklist: screenshots, pricing"))
    assert up.status_code == 201 and up.json()["name"] == "Launch-plan.pdf" and up.json()["chars"] > 10, up.text
    fid = up.json()["id"]
    assert "Launch checklist" in pages[(memory.session_project(mid), f"files/{fid}-Launch-plan.md")]
    assert c.post(f"/api/classes/{mid}/files?name=x.exe", headers=ana, content=b"MZ").status_code == 400
    assert c.post(f"/api/classes/{mid}/files?name=x.pdf", headers=ana, content=b"not a pdf").status_code == 400
    assert c.post(f"/api/classes/{mid}/files?name=x.pdf", headers=eve, content=tiny_pdf("x")).status_code == 404
    assert [f["name"] for f in c.get(f"/api/classes/{mid}/files", headers=host).json()] == ["Launch-plan.pdf"]
    dl = c.get(f"/api/classes/{mid}/files/{fid}", headers=host)
    assert dl.content.startswith(b"%PDF") and dl.headers["content-disposition"].startswith("attachment")
    assert c.get(f"/api/classes/{mid}/files/{fid}", headers=eve).status_code == 404

    def rpc(who_key, method, params=None, id_=1):
        return c.post("/mcp", headers={"Authorization": f"Bearer {who_key}"},
                      json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}).json()

    def tool(who_key, name, args):
        r = rpc(who_key, "tools/call", {"name": name, "arguments": args})["result"]
        return r["isError"], _json.loads(r["content"][0]["text"]) if not r["isError"] else r["content"][0]["text"]

    eve_key = mcp_token(eve)["token"]          # read-only (the default)
    host_key = mcp_token(host, write=True)["token"]
    assert rpc(ana_key, "initialize")["result"]["capabilities"] == {"tools": {}, "resources": {}}
    names = [t["name"] for t in rpc(ana_key, "tools/list")["result"]["tools"]]
    assert names == ["search_memory", "save_note", "add_task", "list_tasks", "update_task",
                     "list_files", "read_file", "add_document"]

    # list_files / resources: only Ana's own projects
    _, listed = tool(ana_key, "list_files", {})
    uris = [f["uri"] for f in listed["files"]]
    assert f"passaly://session-{mid}/recap.md" in uris and f"passaly://session-{mid}/files/{fid}-Launch-plan.md" in uris
    assert all(u.startswith((f"passaly://session-", f"passaly://person-")) for u in uris)
    _, eve_listed = tool(eve_key, "list_files", {})
    assert not any(f"session-{mid}" in f["uri"] for f in eve_listed["files"])
    assert rpc(ana_key, "resources/list")["result"]["resources"][0]["mimeType"] == "text/markdown"

    # read: yours yes; someone else's, or a made-up path, is "not found"
    got = rpc(ana_key, "resources/read", {"uri": f"passaly://session-{mid}/recap.md"})["result"]["contents"][0]
    assert "Deck Friday." in got["text"]
    assert rpc(eve_key, "resources/read", {"uri": f"passaly://session-{mid}/recap.md"})["error"]["code"] == -32002
    assert rpc(ana_key, "resources/read", {"uri": f"passaly://session-{mid}/../x"})["error"]["code"] == -32002
    assert tool(eve_key, "read_file", {"uri": f"passaly://session-{mid}/recap.md"})[0] is True

    # add_document: a member adds one; others in the meeting can read it; outsiders cannot add
    err, doc = tool(ana_key, "add_document", {"session_id": mid, "name": "Pricing notes", "text": "Price: $4.99\nTrial: 7 days"})
    assert not err and doc["name"] == "Pricing-notes.md"
    _, back = tool(host_key, "read_file", {"uri": doc["uri"]})
    assert "Trial: 7 days" in back["text"]
    assert tool(eve_key, "add_document", {"session_id": mid, "name": "x", "text": "y"})[0] is True

    # read-only tokens: no write tools listed, and calling one is refused
    eve_tools = [t["name"] for t in rpc(eve_key, "tools/list")["result"]["tools"]]
    assert not {"save_note", "add_document", "add_task", "update_task"} & set(eve_tools)
    assert {"read_file", "list_tasks"} <= set(eve_tools)
    assert tool(eve_key, "save_note", {"text": "x"}) == (True, "This token is read-only.")

    # revoked or expired: refused; someone else's token cannot be revoked
    tid = c.get("/api/mcp/tokens", headers=eve).json()[0]["id"]
    assert c.delete(f"/api/mcp/tokens/{tid}", headers=ana).status_code == 404
    assert c.delete(f"/api/mcp/tokens/{tid}", headers=eve).status_code == 200
    assert c.post("/mcp", headers={"Authorization": f"Bearer {eve_key}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert c.get("/api/mcp/tokens", headers=eve).json() == []
    old = mcp_token(eve)["token"]
    sqlite3.connect(db_file).execute("update mcp_token set expires_at = '2020-01-01 00:00:00' where token_hash = ?",
                                     (app._token_hash(old),)).connection.commit()
    assert c.post("/mcp", headers={"Authorization": f"Bearer {old}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401

# --- tasks: added in a meeting, kept in memory, done by an agent ---------------------

with TestClient(app.app) as c:
    mid = mt["id"]
    # In the meeting, someone adds a task; everyone in it sees it, outsiders don't.
    t1 = c.post("/api/tasks", headers=ana, json={"title": "Send the deck", "due": "Friday",
                                                  "assignee": "Ana", "session_id": mid}).json()
    assert t1["status"] == "open" and t1["source"] == "app"
    page = pages[(memory.session_project(mid), f"tasks/{t1['id']}.md")]
    assert "# Task: Send the deck" in page and "Status: **open**" in page and f"task_id {t1['id']}" in page
    assert [t["title"] for t in c.get("/api/tasks", headers=host).json()] == ["Send the deck"]
    assert c.get("/api/tasks", headers=eve).json() == []
    assert c.post("/api/tasks", headers=eve, json={"title": "x", "session_id": mid}).status_code == 404
    assert c.patch(f"/api/tasks/{t1['id']}", headers=eve, json={"status": "done"}).status_code == 404
    assert c.patch(f"/api/tasks/{t1['id']}", headers=ana, json={"status": "finished"}).status_code == 400
    # A personal task is its owner's alone.
    mine = c.post("/api/tasks", headers=eve, json={"title": "Eve's own"}).json()
    assert mine["session_id"] is None and [t["title"] for t in c.get("/api/tasks", headers=eve).json()] == ["Eve's own"]
    owners = [k[0] for k in pages if k[1] == f"tasks/{mine['id']}.md"]
    assert len(owners) == 1 and owners[0].startswith("person-")

    # The assistant adds a task from what was asked (Muse calls add_task).
    seen_system = []

    async def task_chat(messages, tools=None):
        seen_system.append(messages[0]["content"])
        if not any(m["role"] == "tool" for m in messages):
            return NS(content="", tool_calls=[NS(id="9", function=NS(name="add_task", arguments=_json.dumps(
                {"title": "Book the launch room", "due": "2026-11-10", "session_id": mid})))])
        return NS(content="Added the task to the Q4 budget meeting.", tool_calls=None)

    app._chat = task_chat
    r = c.post("/api/assistant", headers=ana, json={"messages": [
        {"role": "user", "content": "Add a task in the meeting: book the launch room by Nov 10"}]}).json()
    assert r["actions"][0]["type"] == "task_added" and r["actions"][0]["source"] == "assistant"
    assert f'{mid}: meeting "Q3 budget"' in seen_system[0]  # Muse is told the session ids

    # An agent over MCP: lists open tasks, starts one, finishes it with a result.
    agent = c.post("/api/mcp/tokens", headers=host, json={"name": "OpenCode", "can_write": True}).json()["token"]
    _, listed = tool(agent, "list_tasks", {"status": "open"})
    assert {t["title"] for t in listed["tasks"]} == {"Send the deck", "Book the launch room"}
    tid = next(t["id"] for t in listed["tasks"] if t["title"] == "Send the deck")
    assert tool(agent, "update_task", {"task_id": tid, "status": "in_progress"})[1]["status"] == "in_progress"
    err, done = tool(agent, "update_task", {"task_id": tid, "status": "done", "result": "Sent to all, link: deck.pdf"})
    assert not err and done["status"] == "done"
    page = pages[(memory.session_project(mid), f"tasks/{tid}.md")]
    assert "Status: **done**" in page and "## Result\nSent to all, link: deck.pdf" in page
    _, by_agent = tool(agent, "add_task", {"title": "Draft the release notes", "session_id": mid})
    assert by_agent["source"] == "agent"
    # read-only tokens can list but not change
    ro = c.post("/api/mcp/tokens", headers=ana, json={"name": "Claude"}).json()["token"]
    assert tool(ro, "list_tasks", {})[0] is False
    assert tool(ro, "update_task", {"task_id": tid, "status": "open"}) == (True, "This token is read-only.")
    # the task pages are files too
    _, files = tool(agent, "list_files", {"session_id": mid})
    assert any(f["uri"].endswith(f"/tasks/{tid}.md") for f in files["files"])

memory.URL = ""

# --- Google: sign in, a Meet link with the transcript on, its transcript after ----
import base64 as _b64  # noqa: E402
import json as _json  # noqa: E402


def _jwt(claims):
    return "h." + _b64.urlsafe_b64encode(_json.dumps(claims).encode()).decode().rstrip("=") + ".s"


app.GOOGLE_ID, app.GOOGLE_SECRET = "cid", "secret"
app.GOOGLE_REDIRECT, app.GOOGLE_HOME = "http://testserver/api/google/callback", "http://testserver/"
google_calls, meet_calls = [], []
meet_state = {"ended": False}


async def fake_token(data):
    google_calls.append(data)
    if data["grant_type"] == "authorization_code":
        sub = {"good": "g-ana", "nomeet": "g-bob", "evil": "g-eve"}[data["code"]]
        claims = {"sub": sub, "name": sub[2:].capitalize(), "aud": "other" if sub == "g-eve" else "cid",
                  "iss": "https://accounts.google.com"}
        return {"id_token": _jwt(claims), "refresh_token": "r-" + sub,
                "scope": "openid email" + ("" if sub == "g-bob" else " " + app.MEET_SCOPE)}
    return {"access_token": "a-" + data["refresh_token"]}


async def fake_meet(method, path, access, **kw):
    meet_calls.append((method, path, access, kw))
    if path == "/spaces":
        return {"name": "spaces/s1", "meetingUri": "https://meet.google.com/abc-defg-hij"}
    if path == "/conferenceRecords":
        assert kw["params"]["filter"] == 'space.name = "spaces/s1"'
        return {"conferenceRecords": [{"name": "conferenceRecords/c1", "startTime": "2026-11-04T10:00:00Z",
                                       **({"endTime": "2026-11-04T10:30:00Z"} if meet_state["ended"] else {})}]}
    if path == "/conferenceRecords/c1/transcripts":
        return {"transcripts": [{"name": "conferenceRecords/c1/transcripts/t1", "state": "ENDED"}]}
    if path.endswith("/entries"):
        if not kw["params"].get("pageToken"):
            return {"transcriptEntries": [{"participant": "conferenceRecords/c1/participants/p2", "text": "I'll write the FAQ.",
                                           "startTime": "2026-11-04T10:02:00Z"}], "nextPageToken": "n"}
        return {"transcriptEntries": [{"participant": "conferenceRecords/c1/participants/p1", "text": "Who writes the FAQ?",
                                       "startTime": "2026-11-04T10:01:00Z"}]}
    if path == "/conferenceRecords/c1/participants/p1":
        return {"signedinUser": {"user": "users/1", "displayName": "Ana"}}
    if path == "/conferenceRecords/c1/participants/p2":
        return {"anonymousUser": {"displayName": "Guest Leo"}}
    raise AssertionError(path)


app._google_token, app._meet, app.MEET_POLL_SECONDS = fake_token, fake_meet, 0
app._llm = fake_llm

with TestClient(app.app) as c:
    assert c.get("/api/tutor").json()["google"] is True
    start = c.get("/api/google/start", follow_redirects=False)
    assert start.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in start.headers["location"] and "meetings.space.created" in start.headers["location"]

    def google_signin(code, st=None):
        c.get("/api/google/start", follow_redirects=False)  # each sign-in gets a fresh one-time state
        r = c.get(f"/api/google/callback?code={code}&state={st or c.cookies['pc_gstate']}", follow_redirects=False)
        return r.headers["location"]

    assert google_signin("good", "forged").endswith("#signin-failed")  # state must match the cookie
    c.get("/api/google/start", follow_redirects=False)
    assert c.get(f"/api/google/callback?code=evil&state={c.cookies['pc_gstate']}").status_code == 400  # token for another app
    loc = google_signin("good")
    assert loc.startswith("http://testserver/#signin=")
    ana = {"X-Person-Key": loc.split("#signin=")[1]}
    assert c.get("/api/me", headers=ana).json() == {"id": c.get("/api/me", headers=ana).json()["id"],
                                                    "name": "Ana", "google": True, "meet": True}
    assert google_signin("good") == loc  # same Google account, same person
    bob = {"X-Person-Key": google_signin("nomeet").split("#signin=")[1]}
    assert c.get("/api/me", headers=bob).json()["meet"] is False  # Meet scope unticked
    assert c.post("/api/classes", headers=bob, json={"title": "x", "kind": "meeting", "google_meet": True}).status_code == 409

    m = c.post("/api/classes", headers=ana, json={"title": "FAQ sync", "kind": "meeting", "google_meet": True}).json()
    assert m["call_url"] == "https://meet.google.com/abc-defg-hij" and m["meet"] is True
    assert meet_calls[0][3]["json"]["config"]["artifactConfig"]["transcriptionConfig"]["autoTranscriptionGeneration"] == "ON"
    assert meet_calls[0][2] == "a-r-g-ana"
    # still running: nothing to fetch yet
    assert c.post(f"/api/classes/{m['id']}/meet-transcript", headers=ana).status_code == 409
    meet_state["ended"] = True
    c.post(f"/api/classes/{m['id']}/end", headers=ana)  # the background wait picks it up
    lines = c.get(f"/api/classes/{m['id']}/lines", headers=ana).json()["lines"]
    assert [(l["name"], l["role"], l["text"]) for l in lines] == [
        ("Ana", "teacher", "Who writes the FAQ?"), ("Guest Leo", "student", "I'll write the FAQ.")]
    assert c.get(f"/api/classes/{m['id']}", headers=ana).json()["recap"]  # recap made from Meet's words
    # importing again does not duplicate
    assert c.post(f"/api/classes/{m['id']}/meet-transcript", headers=ana).json() == {"lines": 2}
    assert len(c.get(f"/api/classes/{m['id']}/lines", headers=ana).json()["lines"]) == 2

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

# the image must carry every server module (memory.py was once left out)
from pathlib import Path as _P  # noqa: E402

_copy = next(l for l in (_P(__file__).parent / "Dockerfile").read_text().splitlines() if l.startswith("COPY app.py"))
assert all(f.name in _copy for f in _P(__file__).parent.glob("*.py") if f.name != "test_app.py"), _copy

print("class tutor: ok")
