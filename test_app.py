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

print("class tutor: ok")
