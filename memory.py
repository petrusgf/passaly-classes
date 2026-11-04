"""Shared memory in ai-memory (github.com/akitaonrails/ai-memory), over its MCP
endpoint: memory_write_page / memory_query / memory_read_page.

Every class or meeting is a project ("session-<id>"), every person too
("person-<id>"), so a search only ever looks at projects the asker belongs to.
The cloud assistant (Muse) and the local ones (Llama on a phone, or any MCP
app through /mcp) all read and write the same pages.

Without AI_MEMORY_URL every call is a no-op, so the app runs without it.
"""
import json
import logging
import os

import httpx

URL = os.getenv("AI_MEMORY_URL", "").rstrip("/")  # e.g. http://ai-memory:49374
TOKEN = os.getenv("AI_MEMORY_AUTH_TOKEN", "")
WORKSPACE = os.getenv("AI_MEMORY_WORKSPACE", "passaly-classes")
log = logging.getLogger("memory")


class MemoryError(Exception):
    pass


def session_project(session_id: int) -> str:
    return f"session-{session_id}"


def person_project(user_id: str) -> str:
    return f"person-{user_id}"


async def _call(tool: str, args: dict) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": {"workspace": WORKSPACE, **args}}}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{URL}/mcp", json=body, headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {TOKEN}"})
    r.raise_for_status()
    data = r.json()
    # Errors arrive as JSON-RPC errors or as isError, both with HTTP 200.
    if "error" in data:
        raise MemoryError(data["error"].get("message", "memory error"))
    result = data.get("result", {})
    text = "".join(c.get("text", "") for c in result.get("content", []))
    if result.get("isError"):
        raise MemoryError(text or "memory error")
    return json.loads(text or "{}")


async def write(project: str, path: str, body: str) -> None:
    """Best effort: memory must never break a class, so failures are logged."""
    if not URL:
        return
    try:
        await _call("memory_write_page", {"project": project, "path": path, "body": body})
    except Exception as e:  # noqa: BLE001
        log.warning("memory write %s/%s failed: %s", project, path, e)


async def search(projects: list[str], query: str, limit: int = 6) -> list[dict]:
    """Best pages for `query` across `projects`, each with its full text."""
    if not URL or not query.strip():
        return []
    hits = []
    # ponytail: one query per project, sequential; fine for a person's few
    # dozen classes. A per-person index (or ai-memory's cross-project search)
    # when someone has hundreds.
    for p in projects:
        try:
            found = (await _call("memory_query", {"project": p, "query": query})).get("hits", [])
        except MemoryError:
            continue  # a project with no pages yet does not exist in ai-memory
        except Exception as e:  # noqa: BLE001
            log.warning("memory search %s failed: %s", p, e)
            continue
        hits += [{"project": p, **h} for h in found]
    hits.sort(key=lambda h: h.get("rank", 0))  # ai-memory ranks: lower is better
    out = []
    for h in hits[:limit]:
        try:
            page = await _call("memory_read_page", {"project": h["project"], "path": h["path"]})
        except Exception:  # noqa: BLE001
            continue
        out.append({"project": h["project"], "path": h["path"],
                    "title": page.get("title") or h.get("title", ""),
                    "text": (page.get("body") or "")[:2000]})
    return out
