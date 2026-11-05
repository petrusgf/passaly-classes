"""Live classes and the per-class tutor (hackathon build, Passaly style).

A teacher opens a class, students join with the invite link, and every
browser posts its own speaker's captions as transcript lines. When the class
ends the AI rewrites the teacher's style profile and each student's learning
profile. Afterwards a student asks the class tutor; what the class did not
cover goes to the teacher, and her answer becomes part of that class's pack.

    .venv/bin/uvicorn app:app --reload --port 8010   # http://localhost:8010

Moving into Passaly: the router goes to quiz-api as is, get_person becomes
auth.get_current_user, and the Person table goes away.
"""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import memory
from models import (
    ClassLine, ClassMember, ClassSession, LearnerProfile, McpToken, Person, SessionFile, Task, TutorQuestion,
    create_tables, get_session_factory,
)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///classes.db")
# Any OpenAI-compatible endpoint: OpenAI itself, Gemma/Gemini through Google's
# compatible API, or a local llama.cpp / Ollama server.
TUTOR_MODEL = os.getenv("TUTOR_MODEL", "gpt-4o-mini")
TUTOR_BASE_URL = os.getenv("TUTOR_BASE_URL") or None
TUTOR_API_KEY = os.getenv("TUTOR_API_KEY") or os.getenv("OPENAI_API_KEY", "")

MAX_LINE = 1000
MAX_QUESTION = 1000
MAX_ANSWER = 4000
# ponytail: the whole transcript goes in the prompt. A long class is cut to its
# last MAX_PACK_CHARS; split into chunks and retrieve if classes outgrow it.
MAX_PACK_CHARS = 60000
ASKS_PER_DAY = 50
LANGS = {"en-US", "en-CA", "pt-BR", "es-ES", "fr-CA", "fr-FR"}
KINDS = {"class", "meeting"}
MAX_RECAP = 8000
# Documents added to a class or meeting. Originals on disk, text in memory.
FILES_DIR = Path(os.getenv("FILES_DIR", Path(__file__).parent / "files"))
MAX_FILE = 10 * 1024 * 1024
MAX_FILE_TEXT = 200_000
MAX_PDF_PAGES = 300
FILE_TYPES = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables(DATABASE_URL)
    yield


app = FastAPI(title="Class Tutor", lifespan=lifespan, docs_url=None, redoc_url=None)
router = APIRouter(prefix="/api/classes")


async def get_db() -> AsyncSession:
    async with get_session_factory(DATABASE_URL)() as session:
        yield session


@dataclass
class AnswerUser:
    """Same shape as quiz-api's auth.AnswerUser, so the routes port unchanged."""
    id: str
    username: str
    display_name: str


async def get_person(request: Request, db: AsyncSession = Depends(get_db)) -> AnswerUser:
    key = request.headers.get("X-Person-Key", "")
    p = key and (await db.execute(select(Person).where(Person.key == key))).scalar_one_or_none()
    if not p:
        raise HTTPException(401, "Who are you? Enter your name first")
    return AnswerUser(id=str(p.id), username=p.name, display_name=p.name)


get_current_user = get_person


class NewPerson(BaseModel):
    name: str


@app.post("/api/people", status_code=201)
async def new_person(body: NewPerson, db: AsyncSession = Depends(get_db)):
    p = Person(name=_clean(body.name, 60, "Name"), key=secrets.token_urlsafe(24))
    db.add(p)
    await db.commit()
    return {"id": p.id, "name": p.name, "key": p.key}


def _clean(raw, limit: int, what: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip()
    if not text:
        raise HTTPException(400, f"{what} is empty")
    if len(text) > limit:
        raise HTTPException(400, f"{what} is longer than {limit} characters")
    return text


def _clean_block(raw, limit: int, what: str) -> str:
    """Like _clean, but keeps line breaks (markdown recaps, notes)."""
    text = re.sub(r"[ \t]+", " ", raw or "").strip()
    if not text:
        raise HTTPException(400, f"{what} is empty")
    if len(text) > limit:
        raise HTTPException(400, f"{what} is longer than {limit} characters")
    return text


def _name(user: AnswerUser) -> str:
    return user.display_name or user.username


# --- Google: sign in, Meet links, Meet transcripts --------------------------------
# One OAuth consent gives both: who you are (openid) and the right to create
# Meet links whose transcripts this app can read (meetings.space.created).
# Docs: developers.google.com/identity/openid-connect/openid-connect,
# developers.google.com/workspace/meet/api/guides/artifacts

GOOGLE_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT = os.getenv("GOOGLE_REDIRECT_URI", "https://passaly.com/classes/api/google/callback")
GOOGLE_HOME = GOOGLE_REDIRECT.removesuffix("api/google/callback")
MEET_SCOPE = "https://www.googleapis.com/auth/meetings.space.created"
MEET_API = "https://meet.googleapis.com/v2"
MEET_POLL_SECONDS = 60
MEET_POLLS = 30  # Meet writes the transcript once everyone has left: wait up to 30 min.


async def _google_token(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://oauth2.googleapis.com/token", data=data)
    if r.status_code != 200:
        raise HTTPException(409, "Google refused the sign-in; sign in with Google again")
    return r.json()


async def _meet(method: str, path: str, access: str, **kw) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, MEET_API + path, headers={"Authorization": f"Bearer {access}"}, **kw)
    if r.status_code >= 400:
        msg = (r.json().get("error", {}) if r.headers.get("content-type", "").startswith("application/json") else {})
        raise HTTPException(502, f"Google Meet: {msg.get('message') or r.status_code}")
    return r.json()


def _id_claims(id_token: str) -> dict:
    # It came straight from Google's token endpoint over HTTPS with our secret,
    # so per Google's OpenID guide it needs no signature check; aud/iss still checked.
    part = id_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    if claims.get("aud") != GOOGLE_ID or claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(400, "Not a sign-in for this app")
    return claims


async def _google_access(db, person: Person) -> str:
    if not person or not person.google_refresh:
        raise HTTPException(409, "Sign in with Google to use Google Meet")
    refresh = person.google_refresh
    await db.commit()  # no transaction across Google's calls
    tok = await _google_token({"client_id": GOOGLE_ID, "client_secret": GOOGLE_SECRET,
                               "refresh_token": refresh, "grant_type": "refresh_token"})
    return tok["access_token"]


@app.get("/api/google/start")
async def google_start():
    if not GOOGLE_ID:
        raise HTTPException(404, "Google sign-in is not set up on this server")
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": GOOGLE_ID, "redirect_uri": GOOGLE_REDIRECT, "response_type": "code",
        "scope": f"openid email profile {MEET_SCOPE}", "state": state,
        "access_type": "offline", "prompt": "consent"})
    r = RedirectResponse(url)
    r.set_cookie("pc_gstate", state, max_age=600, httponly=True, samesite="lax",
                 secure=GOOGLE_REDIRECT.startswith("https://"))
    return r


@app.get("/api/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = "",
                          db: AsyncSession = Depends(get_db)):
    if error or not code or not state or state != request.cookies.get("pc_gstate"):
        return RedirectResponse(GOOGLE_HOME + "#signin-failed")
    tok = await _google_token({"code": code, "client_id": GOOGLE_ID, "client_secret": GOOGLE_SECRET,
                               "redirect_uri": GOOGLE_REDIRECT, "grant_type": "authorization_code"})
    claims = _id_claims(tok["id_token"])
    p = (await db.execute(select(Person).where(Person.google_sub == claims["sub"]))).scalar_one_or_none()
    if not p:
        name = (claims.get("name") or claims.get("email") or "Google user")[:60]
        p = Person(name=name, key=secrets.token_urlsafe(24), google_sub=claims["sub"])
        db.add(p)
    # Meet links need the Meet scope; people may untick it on the consent screen.
    if tok.get("refresh_token") and MEET_SCOPE in tok.get("scope", "").split():
        p.google_refresh = tok["refresh_token"]
    await db.commit()
    r = RedirectResponse(GOOGLE_HOME + "#signin=" + p.key)  # the fragment never reaches a server
    r.delete_cookie("pc_gstate")
    return r


@app.get("/api/me")
async def who_am_i(user: AnswerUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Person, int(user.id))
    return {"id": p.id, "name": p.name, "google": bool(p.google_sub), "meet": bool(p.google_refresh)}


async def _new_meet(db, user: AnswerUser) -> dict:
    """A Meet link whose transcript turns on by itself when the host joins."""
    access = await _google_access(db, await db.get(Person, int(user.id)))
    return await _meet("POST", "/spaces", access, json={"config": {"artifactConfig": {
        "transcriptionConfig": {"autoTranscriptionGeneration": "ON"}}}})


def _speaker(p: dict) -> str:
    for kind in ("signedinUser", "anonymousUser", "phoneUser"):
        if p.get(kind, {}).get("displayName"):
            return p[kind]["displayName"]
    return "Someone"


async def _import_meet(db, s: ClassSession) -> int | None:
    """Meet's transcript, speaker by speaker, into the session's lines.
    None: Meet has not finished it yet. 0: there was no transcript."""
    done = (await db.execute(select(func.count()).select_from(ClassLine).where(
        ClassLine.session_id == s.id, ClassLine.user_id.like("meet:%")))).scalar()
    if done:
        return done
    access = await _google_access(db, await db.get(Person, int(s.teacher_id)))
    recs = (await _meet("GET", "/conferenceRecords", access,
                        params={"filter": f'space.name = "{s.meet_space}"'})).get("conferenceRecords", [])
    if not recs or any(not r.get("endTime") for r in recs):
        return None
    entries, names = [], {}
    for rec in sorted(recs, key=lambda r: r["startTime"]):
        trs = (await _meet("GET", f"/{rec['name']}/transcripts", access)).get("transcripts", [])
        # ENDED: stopped, but the text may not be there yet; FILE_GENERATED: complete.
        if any(t.get("state") != "FILE_GENERATED" for t in trs):
            return None
        for t in trs:
            page = ""
            while True:
                d = await _meet("GET", f"/{t['name']}/entries", access,
                                params={"pageSize": 100, **({"pageToken": page} if page else {})})
                entries += d.get("transcriptEntries", [])
                page = d.get("nextPageToken", "")
                if not page:
                    break
    for e in entries:
        if e["participant"] not in names:
            names[e["participant"]] = _speaker(await _meet("GET", f"/{e['participant']}", access))
    entries.sort(key=lambda e: e["startTime"])
    for e in entries:
        name = names[e["participant"]]
        # ponytail: Meet's speakers are not our accounts; the host is matched by name.
        db.add(ClassLine(session_id=s.id, user_id="meet:" + e["participant"].rsplit("/", 1)[-1],
                         name=name, role="teacher" if name == s.teacher_name else "student",
                         text=e["text"], created_at=datetime.fromisoformat(e["startTime"].replace("Z", "+00:00"))))
    await db.commit()
    return len(entries)


async def _after_meet(session_id: int):
    """After a Meet meeting ends: wait for its transcript, then the recap."""
    for _ in range(MEET_POLLS):
        async with get_session_factory(DATABASE_URL)() as db:
            try:
                n = await _import_meet(db, await db.get(ClassSession, session_id))
            except HTTPException:
                n = None
        if n is not None:
            break
        await asyncio.sleep(MEET_POLL_SECONDS)
    await _update_profiles(session_id)


# --- the AI ---------------------------------------------------------------------

async def _chat(messages: list[dict], tools: list[dict] | None = None):
    """One completion; returns the assistant message (content, tool_calls)."""
    if not TUTOR_API_KEY:
        raise HTTPException(503, "The tutor is not configured")
    client = AsyncOpenAI(api_key=TUTOR_API_KEY, base_url=TUTOR_BASE_URL)
    # Meta's Muse Spark "is tuned for the default of 1.0" and its docs say to
    # steer with instructions instead (dev.meta.ai/docs/protocols/chat-completions).
    extra = {} if TUTOR_MODEL.startswith("muse-") else {"temperature": 0.3}
    if tools:
        extra["tools"] = tools
    resp = await client.chat.completions.create(model=TUTOR_MODEL, messages=messages, **extra)
    return resp.choices[0].message


async def _llm(system: str, user: str) -> str:
    msg = await _chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
    return (msg.content or "").strip()


def build_pack(session, lines, answered, teacher_style: str, student_profile: str) -> str:
    """Everything the tutor may use for one class, newest transcript kept."""
    transcript = "\n".join(f"{l.name} ({l.role}): {l.text}" for l in lines)
    if len(transcript) > MAX_PACK_CHARS:
        transcript = transcript[-MAX_PACK_CHARS:]
    extra = "\n\n".join(f"Q: {q.question}\n{session.teacher_name}: {q.teacher_answer}"
                        for q in answered)
    return (
        f"CLASS: {session.title}" + (f" ({session.subject})" if session.subject else "") +
        f"\nTEACHER: {session.teacher_name}"
        f"\n\n[HOW {session.teacher_name.upper()} TEACHES]\n{teacher_style or 'Not known yet.'}"
        f"\n\n[HOW THIS STUDENT LEARNS]\n{student_profile or 'Not known yet.'}"
        f"\n\n[TRANSCRIPT]\n{transcript or '(empty)'}"
        f"\n\n[THE TEACHER'S LATER ANSWERS]\n{extra or '(none)'}"
    )


TUTOR_SYSTEM = (
    "You are the after-class tutor for {teacher}'s class. Explain the way {teacher} "
    "does: reuse her examples, analogies and tone from the material. Adapt to how "
    "this student learns. Answer in the language the student asks in.\n"
    "Use ONLY the material: the transcript and the teacher's later answers. If they "
    "do not cover the question, do not guess.\n"
    "Reply in exactly this format:\n"
    "IN_CLASS: yes or no\n"
    "ANSWER: your answer (if no, say kindly that you are asking {teacher} and she will reply)"
)


def parse_tutor(raw: str) -> tuple[bool, str]:
    """(from_class, answer). Anything unreadable goes to the teacher: a wrong
    'yes' would hide a question she should have seen."""
    m = re.search(r"IN_CLASS:\s*(yes|no)\b", raw, re.I)
    a = re.search(r"ANSWER:\s*(.*)", raw, re.I | re.S)
    answer = (a.group(1) if a else raw).strip()
    return bool(m and m.group(1).lower() == "yes"), answer


STYLE_SYSTEM = (
    "You keep a short profile (max 150 words) of how a teacher teaches: the "
    "examples and analogies she likes, how she checks understanding, pace, level "
    "of detail, language mix, tone. Update the profile with what this class "
    "shows. Reply with the profile only."
)
RECAP_SYSTEM = (
    "You write the recap of a {kind} from its transcript, in the transcript's "
    "language, as markdown with exactly these sections:\n"
    "## Summary\n(at most 120 words)\n## Decisions\n## Action items\n"
    "(one per line: '- Owner: task (due: date)', the due part only if it was said)\n"
    "## Open questions\n"
    "Write 'None.' under an empty section. Use only the transcript; never invent "
    "owners, dates or decisions."
)
LEARNER_SYSTEM = (
    "You keep a short profile (max 120 words) of how one student learns: what "
    "confuses them, which explanations worked, their level, the language they "
    "prefer, their goals. Update it with this class and their questions. Reply "
    "with the profile only. Do not include personal data beyond learning."
)


async def _profile(db, user_id: str, kind: str) -> str:
    row = (await db.execute(select(LearnerProfile).where(
        LearnerProfile.user_id == user_id, LearnerProfile.kind == kind))).scalar_one_or_none()
    return row.text if row else ""


async def _save_profile(db, user_id: str, kind: str, text: str):
    row = (await db.execute(select(LearnerProfile).where(
        LearnerProfile.user_id == user_id, LearnerProfile.kind == kind))).scalar_one_or_none()
    if row:
        row.text, row.updated_at = text, datetime.now(timezone.utc)
    else:
        db.add(LearnerProfile(user_id=user_id, kind=kind, text=text))
    title = "How I teach / host" if kind == "teacher" else "How I learn"
    await memory.write(memory.person_project(user_id), f"profile-{kind}.md", f"# {title}\n\n{text}")


def _transcript_page(s: ClassSession, members, lines) -> str:
    who = ", ".join(f"{m.name} ({_role_label(s, m.role)})" for m in members)
    body = "\n".join(f"- {l.name}: {l.text}" for l in lines) or "(empty)"
    return (f"# {s.title}\n\n{s.kind.capitalize()} on {s.created_at:%Y-%m-%d}, "
            f"{_role_label(s, 'teacher')}: {s.teacher_name}. People: {who}.\n\n## Transcript\n{body}\n")


def _recap_page(s: ClassSession) -> str:
    return f"# Recap: {s.title} ({s.created_at:%Y-%m-%d})\n\n{s.recap}\n"


def _role_label(s: ClassSession, role: str) -> str:
    if s.kind == "meeting":
        return "host" if role == "teacher" else "participant"
    return role


async def _remember_session(db, s: ClassSession):
    """The transcript (and recap, when there is one) into the shared memory."""
    members = (await db.execute(select(ClassMember).where(
        ClassMember.session_id == s.id).order_by(ClassMember.id))).scalars().all()
    lines = (await db.execute(select(ClassLine).where(
        ClassLine.session_id == s.id).order_by(ClassLine.id))).scalars().all()
    project = memory.session_project(s.id)
    await memory.write(project, "transcript.md", _transcript_page(s, members, lines))
    if s.recap:
        await memory.write(project, "recap.md", _recap_page(s))


async def _update_profiles(session_id: int):
    """After it ends: the recap, the host's style, one profile per student,
    all into the shared memory too. Without a server AI only the transcript is
    saved; the phones then write recap and profiles on-device."""
    async with get_session_factory(DATABASE_URL)() as db:
        s = await db.get(ClassSession, session_id)
        lines = (await db.execute(select(ClassLine).where(
            ClassLine.session_id == session_id).order_by(ClassLine.id))).scalars().all()
        transcript = "\n".join(f"{l.name} ({_role_label(s, l.role)}): {l.text}" for l in lines)[-MAX_PACK_CHARS:]
        await db.commit()
        if not TUTOR_API_KEY or not transcript:
            await _remember_session(db, s)
            return
        recap = await _llm(RECAP_SYSTEM.format(kind=s.kind), f"{s.kind.upper()}: {s.title}\n\n{transcript}")
        s.recap = recap[:MAX_RECAP] or None
        await db.commit()
        await _remember_session(db, s)
        await _recap_tasks(db, s)
        # Each AI call takes seconds: never hold a transaction (and SQLite's
        # lock) across one, or students asking meanwhile get "database is locked".
        old = await _profile(db, s.teacher_id, "teacher")
        await db.commit()
        text = await _llm(STYLE_SYSTEM, f"CURRENT PROFILE:\n{old or '(none)'}\n\nCLASS TRANSCRIPT:\n{transcript}")
        await _save_profile(db, s.teacher_id, "teacher", text)
        await db.commit()
        students = (await db.execute(select(ClassMember).where(
            ClassMember.session_id == session_id, ClassMember.role == "student"))).scalars().all()
        for m in students:
            asked = (await db.execute(select(TutorQuestion.question).where(
                TutorQuestion.session_id == session_id,
                TutorQuestion.student_id == m.user_id))).scalars().all()
            old = await _profile(db, m.user_id, "student")
            await db.commit()
            text = await _llm(
                LEARNER_SYSTEM,
                f"STUDENT: {m.name}\nCURRENT PROFILE:\n{old or '(none)'}\n\n"
                f"CLASS TRANSCRIPT:\n{transcript}\n\nTHEIR TUTOR QUESTIONS:\n" +
                ("\n".join(asked) or "(none)"))
            await _save_profile(db, m.user_id, "student", text)
            await db.commit()


# --- membership -----------------------------------------------------------------

async def _member(db, session_id: int, user: AnswerUser):
    s = await db.get(ClassSession, session_id)
    m = (await db.execute(select(ClassMember).where(
        ClassMember.session_id == session_id, ClassMember.user_id == user.id))).scalar_one_or_none()
    if not s or not m:
        raise HTTPException(404, "Class not found")
    return s, m


def _session_out(s: ClassSession, role: str) -> dict:
    out = {"id": s.id, "title": s.title, "subject": s.subject, "lang": s.lang,
           "call_url": s.call_url, "status": s.status, "teacher": s.teacher_name,
           "role": role, "kind": s.kind, "recap": s.recap, "meet": bool(s.meet_space),
           "created_at": s.created_at.isoformat()}
    if role == "teacher":
        out["invite_token"] = s.invite_token
    return out


class NewClass(BaseModel):
    title: str
    subject: str | None = None
    lang: str = "en-US"
    call_url: str | None = None
    kind: str = "class"
    google_meet: bool = False  # create a Meet link with the transcript on


@router.post("", status_code=201)
async def create_class(body: NewClass, user: AnswerUser = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    if body.lang not in LANGS:
        raise HTTPException(400, f"lang must be one of {sorted(LANGS)}")
    if body.kind not in KINDS:
        raise HTTPException(400, "kind is class or meeting")
    url = (body.call_url or "").strip() or None
    if url and not url.startswith("https://"):
        raise HTTPException(400, "The call link must start with https://")
    space = await _new_meet(db, user) if body.google_meet else None
    if space:
        url = space["meetingUri"]
    s = ClassSession(meet_space=space and space["name"], teacher_id=user.id, teacher_name=_name(user),
                     title=_clean(body.title, 200, "Title"),
                     subject=(body.subject or "").strip()[:100] or None,
                     lang=body.lang, call_url=url, invite_token=secrets.token_urlsafe(12),
                     kind=body.kind)
    db.add(s)
    await db.flush()
    db.add(ClassMember(session_id=s.id, user_id=user.id, name=_name(user), role="teacher"))
    await db.commit()
    return _session_out(s, "teacher")


@router.get("")
async def my_classes(user: AnswerUser = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ClassSession, ClassMember.role).join(ClassMember, ClassMember.session_id == ClassSession.id)
        .where(ClassMember.user_id == user.id).order_by(ClassSession.id.desc()))).all()
    return [_session_out(s, role) for s, role in rows]


@router.post("/join/{token}")
async def join_class(token: str, user: AnswerUser = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    s = (await db.execute(select(ClassSession).where(
        ClassSession.invite_token == token))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Invite not found")
    m = (await db.execute(select(ClassMember).where(
        ClassMember.session_id == s.id, ClassMember.user_id == user.id))).scalar_one_or_none()
    if not m:
        m = ClassMember(session_id=s.id, user_id=user.id, name=_name(user), role="student")
        db.add(m)
        await db.commit()
    return _session_out(s, m.role)


@router.get("/{session_id}")
async def get_class(session_id: int, user: AnswerUser = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    s, m = await _member(db, session_id, user)
    members = (await db.execute(select(ClassMember).where(
        ClassMember.session_id == session_id).order_by(ClassMember.id))).scalars().all()
    return {**_session_out(s, m.role), "members": [{"name": x.name, "role": x.role} for x in members]}


# --- transcript -----------------------------------------------------------------

class Line(BaseModel):
    text: str


@router.post("/{session_id}/lines", status_code=201)
async def add_line(session_id: int, body: Line, user: AnswerUser = Depends(get_current_user),
                   db: AsyncSession = Depends(get_db)):
    s, m = await _member(db, session_id, user)
    if s.status != "live":
        raise HTTPException(409, "The class has ended")
    line = ClassLine(session_id=s.id, user_id=user.id, name=m.name, role=m.role,
                     text=_clean(body.text, MAX_LINE, "Line"))
    db.add(line)
    await db.commit()
    return {"id": line.id}


@router.get("/{session_id}/lines")
async def get_lines(session_id: int, after: int = 0, user: AnswerUser = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    """Polled every few seconds during class: only lines newer than `after`."""
    s, _ = await _member(db, session_id, user)
    rows = (await db.execute(select(ClassLine).where(
        ClassLine.session_id == session_id, ClassLine.id > after)
        .order_by(ClassLine.id).limit(500))).scalars().all()
    return {"status": s.status, "lines": [
        {"id": l.id, "name": l.name, "role": l.role, "text": l.text,
         "at": l.created_at.isoformat()} for l in rows]}


@router.post("/{session_id}/end")
async def end_class(session_id: int, tasks: BackgroundTasks,
                    user: AnswerUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    s, m = await _member(db, session_id, user)
    if m.role != "teacher":
        raise HTTPException(403, "Only the teacher can end the class")
    if s.status == "live":
        s.status = "ended"
        await db.commit()
        tasks.add_task(_after_meet if s.meet_space else _update_profiles, s.id)
    return _session_out(s, m.role)


@router.post("/{session_id}/meet-transcript")
async def import_meet_transcript(session_id: int, tasks: BackgroundTasks,
                                 user: AnswerUser = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    """Fetch Meet's transcript now, e.g. when the automatic wait gave up."""
    s, m = await _member(db, session_id, user)
    if m.role != "teacher" or not s.meet_space:
        raise HTTPException(404, "No Google Meet transcript for this meeting")
    n = await _import_meet(db, s)
    if n is None:
        raise HTTPException(409, "Meet has not finished the transcript yet. Try again in a few minutes")
    if n and s.status == "live":
        s.status = "ended"
        await db.commit()
    if n:
        tasks.add_task(_update_profiles, s.id)
    return {"lines": n}


# --- tutor ----------------------------------------------------------------------

def _question_out(q: TutorQuestion) -> dict:
    return {"id": q.id, "session_id": q.session_id, "student": q.student_name,
            "question": q.question, "ai_answer": q.ai_answer, "from_class": q.from_class,
            "teacher_answer": q.teacher_answer, "status": q.status,
            "created_at": q.created_at.isoformat()}


class Ask(BaseModel):
    question: str


@app.get("/api/tutor")
async def tutor_info():
    """Whether this server has a cloud AI, so the app can offer it."""
    return {"cloud": bool(TUTOR_API_KEY), "model": TUTOR_MODEL if TUTOR_API_KEY else None,
            "google": bool(GOOGLE_ID)}


@router.post("/{session_id}/ask")
async def ask_tutor(session_id: int, body: Ask, user: AnswerUser = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    s, m = await _member(db, session_id, user)
    if m.role != "student":
        raise HTTPException(403, "The tutor is for the class's students")
    question = _clean(body.question, MAX_QUESTION, "Question")
    today = (await db.execute(select(func.count()).select_from(TutorQuestion).where(
        TutorQuestion.student_id == user.id,
        TutorQuestion.created_at > datetime.now(timezone.utc) - timedelta(days=1)))).scalar()
    if today >= ASKS_PER_DAY:
        raise HTTPException(429, f"You can ask {ASKS_PER_DAY} questions a day")
    lines = (await db.execute(select(ClassLine).where(
        ClassLine.session_id == s.id).order_by(ClassLine.id))).scalars().all()
    answered = (await db.execute(select(TutorQuestion).where(
        TutorQuestion.session_id == s.id, TutorQuestion.teacher_answer.is_not(None))
        .order_by(TutorQuestion.id))).scalars().all()
    pack = build_pack(s, lines, answered, await _profile(db, s.teacher_id, "teacher"),
                      await _profile(db, user.id, "student"))
    await db.commit()  # no transaction stays open across the AI call (seconds)
    raw = await _llm(TUTOR_SYSTEM.format(teacher=s.teacher_name),
                     f"{pack}\n\n[STUDENT {m.name} ASKS]\n{question}")
    from_class, answer = parse_tutor(raw)
    q = TutorQuestion(session_id=s.id, student_id=user.id, student_name=m.name,
                      question=question, ai_answer=answer, from_class=from_class,
                      status="answered" if from_class else "waiting_teacher")
    db.add(q)
    await db.commit()
    return _question_out(q)


@router.get("/{session_id}/questions")
async def class_questions(session_id: int, user: AnswerUser = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """A student sees their own questions; the teacher sees everyone's."""
    s, m = await _member(db, session_id, user)
    stmt = select(TutorQuestion).where(TutorQuestion.session_id == s.id)
    if m.role != "teacher":
        stmt = stmt.where(TutorQuestion.student_id == user.id)
    rows = (await db.execute(stmt.order_by(TutorQuestion.id.desc()))).scalars().all()
    return [_question_out(q) for q in rows]


@router.get("/inbox/waiting")
async def teacher_inbox(user: AnswerUser = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    """Questions the tutor could not answer, across all of the teacher's classes."""
    rows = (await db.execute(
        select(TutorQuestion, ClassSession.title).join(ClassSession, ClassSession.id == TutorQuestion.session_id)
        .where(ClassSession.teacher_id == user.id, TutorQuestion.status == "waiting_teacher")
        .order_by(TutorQuestion.id))).all()
    return [{**_question_out(q), "class_title": title} for q, title in rows]


class TeacherAnswer(BaseModel):
    text: str


@router.post("/questions/{question_id}/answer")
async def answer_question(question_id: int, body: TeacherAnswer,
                          user: AnswerUser = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    q = await db.get(TutorQuestion, question_id)
    s = q and await db.get(ClassSession, q.session_id)
    if not q or s.teacher_id != user.id:
        raise HTTPException(404, "Question not found")
    q.teacher_answer = _clean(body.text, MAX_ANSWER, "Answer")
    q.status = "teacher_answered"
    await db.commit()
    await memory.write(memory.session_project(s.id), f"answers/q{q.id}.md",
                       f"# {q.question}\n\n{s.teacher_name} answered: {q.teacher_answer}\n")
    return _question_out(q)


# --- for the phone app: the AI runs on the phone, the server only keeps data ---------

@router.get("/{session_id}/pack")
async def class_pack(session_id: int, user: AnswerUser = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    """Everything the on-device tutor needs for one class."""
    s, m = await _member(db, session_id, user)
    lines = (await db.execute(select(ClassLine).where(
        ClassLine.session_id == s.id).order_by(ClassLine.id))).scalars().all()
    answered = (await db.execute(select(TutorQuestion).where(
        TutorQuestion.session_id == s.id, TutorQuestion.teacher_answer.is_not(None))
        .order_by(TutorQuestion.id))).scalars().all()
    return {
        **_session_out(s, m.role),
        "lines": [{"name": l.name, "role": l.role, "text": l.text} for l in lines],
        "teacher_answers": [{"question": q.question, "answer": q.teacher_answer} for q in answered],
        "teacher_style": await _profile(db, s.teacher_id, "teacher"),
        "my_profile": await _profile(db, user.id, m.role),
    }


class OnDeviceAnswer(BaseModel):
    question: str
    ai_answer: str
    from_class: bool


@router.post("/{session_id}/questions", status_code=201)
async def record_question(session_id: int, body: OnDeviceAnswer,
                          user: AnswerUser = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """A question the phone already answered (or could not): saved so the
    teacher sees it, and sent to her inbox when the class did not cover it."""
    s, m = await _member(db, session_id, user)
    if m.role != "student":
        raise HTTPException(403, "The tutor is for the class's students")
    q = TutorQuestion(session_id=s.id, student_id=user.id, student_name=m.name,
                      question=_clean(body.question, MAX_QUESTION, "Question"),
                      ai_answer=_clean(body.ai_answer, MAX_ANSWER, "Answer"),
                      from_class=body.from_class,
                      status="answered" if body.from_class else "waiting_teacher")
    db.add(q)
    await db.commit()
    return _question_out(q)


class ProfileText(BaseModel):
    text: str


@app.put("/api/profiles/{kind}")
async def put_profile(kind: str, body: ProfileText, user: AnswerUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    """The phone writes its own person's profile: teacher style or learner."""
    if kind not in ("teacher", "student"):
        raise HTTPException(400, "kind is teacher or student")
    await _save_profile(db, user.id, kind, _clean(body.text, 2000, "Profile"))
    await db.commit()
    return {"ok": True}



class RecapText(BaseModel):
    text: str


@router.put("/{session_id}/recap")
async def put_recap(session_id: int, body: RecapText, user: AnswerUser = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    """The host's phone wrote the recap with Llama (no server AI needed)."""
    s, m = await _member(db, session_id, user)
    if m.role != "teacher":
        raise HTTPException(403, "Only the host writes the recap")
    if s.status != "ended":
        raise HTTPException(409, "End it first")
    s.recap = _clean_block(body.text, MAX_RECAP, "Recap")
    await db.commit()
    await _remember_session(db, s)
    await _recap_tasks(db, s)
    return _session_out(s, m.role)


# --- shared memory: the same pages for the cloud assistant and local ones ----------

async def _my_projects(db, user: AnswerUser, session_id: int | None = None) -> list[str]:
    """The asker's own pages, then their classes and meetings, newest first."""
    q = select(ClassMember.session_id).where(ClassMember.user_id == user.id)
    if session_id:
        q = q.where(ClassMember.session_id == session_id)
    ids = (await db.execute(q.order_by(ClassMember.session_id.desc()).limit(30))).scalars().all()
    return [memory.person_project(user.id)] + [memory.session_project(i) for i in ids]


async def _search_memory(db, user: AnswerUser, query: str, session_id: int | None = None) -> list[dict]:
    """Keyword search; when nothing matches, the newest recaps instead, so a
    question worded unlike the meeting ("what do I deliver?") still has context."""
    query = _clean(query, 300, "Search")
    projects = await _my_projects(db, user, session_id)
    return (await memory.search(projects, query)
            or await memory.recent(projects[1:], "recap.md", RECENT_RECAPS))


async def _save_note(user: AnswerUser, text: str) -> dict:
    text = _clean_block(text, MAX_ANSWER, "Note")
    path = f"notes/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.md"
    await memory.write(memory.person_project(user.id), path, f"# Note\n\n{text}\n")
    return {"saved": path}


@app.get("/api/memory/search")
async def memory_search(q: str, session_id: int | None = None,
                        user: AnswerUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """For local assistants (Llama on the phone): the pages that match, in full."""
    return {"hits": await _search_memory(db, user, q, session_id)}


class Note(BaseModel):
    text: str


@app.post("/api/memory/notes", status_code=201)
async def memory_note(body: Note, user: AnswerUser = Depends(get_current_user)):
    return await _save_note(user, body.text)



# --- tasks: one directory, mirrored into memory so agents can read and do them ----------

TASK_STATUSES = ("open", "in_progress", "done")
TASK_SOURCES = ("app", "assistant", "agent")


def _task_out(t: Task) -> dict:
    return {"id": t.id, "session_id": t.session_id, "title": t.title, "details": t.details,
            "assignee": t.assignee, "due": t.due, "status": t.status, "result": t.result,
            "source": t.source, "owner": t.owner_name,
            "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat()}


def _task_project(t: Task) -> str:
    return memory.session_project(t.session_id) if t.session_id else memory.person_project(t.owner_id)


async def _remember_task(db, t: Task):
    """tasks/<id>.md: the task as an agent should read it, rewritten on every change."""
    where = "Personal task"
    if t.session_id:
        s = await db.get(ClassSession, t.session_id)
        where = f"From the {s.kind} \"{s.title}\" ({s.created_at:%Y-%m-%d})"
    rows = [f"- Status: **{t.status}**", f"- Task id: {t.id}", f"- Added by: {t.owner_name} (via {t.source})"]
    if t.assignee:
        rows.append(f"- For: {t.assignee}")
    if t.due:
        rows.append(f"- Due: {t.due}")
    body = (f"# Task: {t.title}\n\n{where}.\n\n" + "\n".join(rows) +
            (f"\n\n## Details\n{t.details}" if t.details else "") +
            (f"\n\n## Result\n{t.result}" if t.result else "") +
            f"\n\nAgents: when you work on it, call update_task with task_id {t.id} "
            "(status in_progress, then done with a result).\n")
    await memory.write(_task_project(t), f"tasks/{t.id}.md", body)


async def _task_scope(db, user: AnswerUser, t: Task):
    """A session's tasks are its members'; a personal task only its owner's."""
    if t.session_id:
        await _member(db, t.session_id, user)
    elif t.owner_id != user.id:
        raise HTTPException(404, "Task not found")


async def _add_task(db, user: AnswerUser, title, details=None, assignee=None, due=None,
                    session_id=None, source="app") -> Task:
    if session_id is not None:
        if not isinstance(session_id, int):
            raise HTTPException(400, "session_id is a number")
        await _member(db, session_id, user)
    t = Task(session_id=session_id, owner_id=user.id, owner_name=_name(user),
             title=_clean(str(title or ""), 200, "Title"),
             details=_clean_block(str(details), MAX_ANSWER, "Details") if details else None,
             assignee=_clean(str(assignee), 60, "For") if assignee else None,
             due=_clean(str(due), 60, "Due") if due else None,
             source=source if source in TASK_SOURCES else "app")
    db.add(t)
    await db.commit()
    await _remember_task(db, t)
    return t


RECAP_TASK_NOTE = "From the recap"


def action_items(recap: str) -> list[tuple[str, str | None, str | None]]:
    """The recap's "## Action items" lines ("- Owner: task (due: date)") as
    (task, owner, due). "None." and other non-items are skipped."""
    m = re.search(r"^##\s*Action items\s*$(.*?)(?=^##|\Z)", recap or "", re.S | re.M | re.I)
    out = []
    for line in (m.group(1).splitlines() if m else []):
        t = line.strip()
        if not t.startswith(("-", "*", "•")):
            continue
        t = t.lstrip("-*• ").strip()
        if t.lower().rstrip(".") in ("", "none", "nenhum", "nenhuma", "ninguno", "aucun"):
            continue
        due = None
        d = re.search(r"\(\s*(?:due|prazo|até|fecha)\s*:?\s*([^)]+)\)\s*\.?$", t, re.I)
        if d:
            due, t = d.group(1).strip(), t[:d.start()].strip()
        owner = None
        o = re.match(r"([^:]{1,60}):\s*(.+)", t)
        if o:
            owner, t = o.group(1).strip(), o.group(2).strip()
        if t:
            out.append((t.rstrip(".")[:200], owner, due))
    return out


async def _recap_tasks(db, s: ClassSession) -> int:
    """Each action item in the recap becomes a task in that class or meeting,
    added by the assistant for the host. Once per session."""
    done = (await db.execute(select(func.count()).select_from(Task).where(
        Task.session_id == s.id, Task.source == "assistant",
        Task.details.like(f"{RECAP_TASK_NOTE}%")))).scalar()
    if done:
        return 0
    host = AnswerUser(id=s.teacher_id, username=s.teacher_name, display_name=s.teacher_name)
    n = 0
    for title, owner, due in action_items(s.recap):
        await _add_task(db, host, title, details=f"{RECAP_TASK_NOTE} of \"{s.title}\".",
                        assignee=owner, due=due, session_id=s.id, source="assistant")
        n += 1
    return n


async def _update_task(db, user: AnswerUser, task_id, status=None, result=None) -> Task:
    t = await db.get(Task, task_id) if isinstance(task_id, int) else None
    if not t:
        raise HTTPException(404, "Task not found")
    await _task_scope(db, user, t)
    if status is not None:
        if status not in TASK_STATUSES:
            raise HTTPException(400, f"status is one of {', '.join(TASK_STATUSES)}")
        t.status = status
    if result is not None:
        t.result = _clean_block(str(result), MAX_ANSWER, "Result")
    t.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await _remember_task(db, t)
    return t


async def _my_tasks(db, user: AnswerUser, status=None, session_id=None) -> list[Task]:
    member_of = select(ClassMember.session_id).where(ClassMember.user_id == user.id)
    q = select(Task).where((Task.session_id.in_(member_of)) |
                           ((Task.session_id.is_(None)) & (Task.owner_id == user.id)))
    if status:
        q = q.where(Task.status == status)
    if session_id is not None:
        q = q.where(Task.session_id == session_id)
    return (await db.execute(q.order_by(Task.id.desc()).limit(200))).scalars().all()


class NewTask(BaseModel):
    title: str
    details: str | None = None
    assignee: str | None = None
    due: str | None = None
    session_id: int | None = None


class TaskChange(BaseModel):
    status: str | None = None
    result: str | None = None


@app.post("/api/tasks", status_code=201)
async def create_task(body: NewTask, user: AnswerUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    return _task_out(await _add_task(db, user, body.title, body.details, body.assignee, body.due,
                                     body.session_id, "app"))


@app.get("/api/tasks")
async def list_tasks(status: str | None = None, session_id: int | None = None,
                     user: AnswerUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return [_task_out(t) for t in await _my_tasks(db, user, status, session_id)]


@app.patch("/api/tasks/{task_id}")
async def change_task(task_id: int, body: TaskChange, user: AnswerUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    return _task_out(await _update_task(db, user, task_id, body.status, body.result))


async def _run_server_tool(db, user: AnswerUser, name: str, args: dict, source: str,
                           session_id: int | None = None):
    """The tools that touch shared data, the same for the assistant and MCP.
    Returns the result, or None when `name` is not one of them."""
    if name == "search_memory":
        return {"hits": await _search_memory(db, user, str(args.get("query", "")), session_id)}
    if name == "save_note":
        return await _save_note(user, str(args.get("text", "")))
    if name == "add_task":
        return _task_out(await _add_task(db, user, args.get("title"), args.get("details"), args.get("assignee"),
                                         args.get("due"), args.get("session_id"), source))
    if name == "list_tasks":
        sid = args.get("session_id")
        return {"tasks": [_task_out(t) for t in await _my_tasks(
            db, user, args.get("status") or None, sid if isinstance(sid, int) else None)]}
    if name == "update_task":
        return _task_out(await _update_task(db, user, args.get("task_id"), args.get("status"), args.get("result")))
    return None


# --- the assistant: Muse with tools; the phone runs the device ones -----------------

# Run here: they touch the shared memory.
SERVER_TOOLS = {
    "search_memory": {"description": "Keyword search over the user's classes, meetings, recaps and "
                                     "notes. Use a few specific words likely said in the meeting "
                                     "(names, topics); with no match it returns the latest recaps.",
                      "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "save_note": {"description": "Save a note to the user's memory.",
                  "properties": {"text": {"type": "string"}}, "required": ["text"]},
    "add_task": {"description": "Add a task to the task list: to a class or meeting (session_id, "
                                "everyone in it sees it) or the user's own (no session_id).",
                 "properties": {"title": {"type": "string"}, "details": {"type": "string"},
                                "assignee": {"type": "string", "description": "who does it, if said"},
                                "due": {"type": "string", "description": "as said, or YYYY-MM-DD"},
                                "session_id": {"type": "integer"}},
                 "required": ["title"]},
    "list_tasks": {"description": "List tasks the user can see, newest first.",
                   "properties": {"status": {"type": "string", "enum": ["open", "in_progress", "done"]},
                                  "session_id": {"type": "integer"}},
                   "required": []},
    "update_task": {"description": "Change a task's status (open, in_progress, done) and/or record "
                                   "its result: what was done, links, output.",
                    "properties": {"task_id": {"type": "integer"},
                                   "status": {"type": "string", "enum": ["open", "in_progress", "done"]},
                                   "result": {"type": "string"}},
                    "required": ["task_id"]},
}
# Proposed to the phone, which runs them only after the user confirms.
DEVICE_TOOLS = {
    "add_calendar_event": {
        "description": "Add an event or reminder to the user's calendar.",
        "properties": {"title": {"type": "string"},
                       "start": {"type": "string", "description": "ISO 8601 with UTC offset"},
                       "duration_minutes": {"type": "integer"},
                       "description": {"type": "string"}},
        "required": ["title", "start"]},
    "share_text": {"description": "Share text (e.g. a recap) through another app: email, WhatsApp…",
                   "properties": {"text": {"type": "string"}}, "required": ["text"]},
}
TOOLS = [{"type": "function", "function": {
    "name": n, "description": t["description"],
    "parameters": {"type": "object", "properties": t["properties"], "required": t["required"]}}}
    for n, t in {**SERVER_TOOLS, **DEVICE_TOOLS}.items()]

ASSISTANT_SYSTEM = (
    "You are {name}'s Passaly assistant for their classes and meetings. Now: {now}.\n"
    "Before answering about anything said or decided in a class or meeting, call "
    "search_memory, and answer only from what it returns; if it finds nothing, say so.\n"
    "When the user wants something done, call add_calendar_event, share_text, "
    "save_note or add_task. A task from a class or meeting gets its session_id from "
    "this list (newest first): {sessions}. Calendar and share run on the user's "
    "phone after they confirm, so say what you proposed. Reply briefly, in the "
    "user's language."
)
MAX_TOOL_ROUNDS = 4
RECENT_RECAPS = 3
# ponytail: per-process daily count; a table when there is more than one worker.
_assist_count: dict[tuple[str, str], int] = {}


class ChatTurn(BaseModel):
    role: str
    content: str


class AssistantAsk(BaseModel):
    messages: list[ChatTurn]
    now: str | None = None  # the phone's local time, ISO 8601 with offset
    session_id: int | None = None


def _device_action(name: str, args: dict) -> dict | None:
    """A device tool call the phone can show, or None when the arguments are unusable."""
    if name == "add_calendar_event":
        title, start = str(args.get("title", "")).strip(), str(args.get("start", "")).strip()
        try:
            datetime.fromisoformat(start)
        except ValueError:
            return None
        if not title:
            return None
        minutes = args.get("duration_minutes")
        return {"type": name, "title": title[:200], "start": start,
                "duration_minutes": minutes if isinstance(minutes, int) and 0 < minutes <= 24 * 60 else 30,
                "description": str(args.get("description", ""))[:2000]}
    if name == "share_text":
        text = str(args.get("text", "")).strip()
        return {"type": name, "text": text[:MAX_RECAP]} if text else None
    return None


@app.post("/api/assistant")
async def assistant(body: AssistantAsk, user: AnswerUser = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    """Cloud mode. Returns the reply and the device actions waiting for the user's OK."""
    day = (user.id, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    if _assist_count.get(day, 0) >= ASKS_PER_DAY:
        raise HTTPException(429, f"You can ask {ASKS_PER_DAY} times a day")
    _assist_count[day] = _assist_count.get(day, 0) + 1
    turns = [t for t in body.messages if t.role in ("user", "assistant")][-12:]
    if not turns or turns[-1].role != "user":
        raise HTTPException(400, "The last message must be the user's")
    recent = (await db.execute(
        select(ClassSession).join(ClassMember, ClassMember.session_id == ClassSession.id)
        .where(ClassMember.user_id == user.id).order_by(ClassSession.id.desc()).limit(10))).scalars().all()
    sessions = "; ".join(f"{s.id}: {s.kind} \"{s.title}\" {s.created_at:%Y-%m-%d}" for s in recent) or "none"
    messages = [{"role": "system", "content": ASSISTANT_SYSTEM.format(
        name=_name(user), now=(body.now or datetime.now(timezone.utc).isoformat())[:40], sessions=sessions)}]
    messages += [{"role": t.role, "content": _clean_block(t.content, MAX_QUESTION, "Message")} for t in turns]
    await db.commit()  # no transaction across the AI calls
    actions, sources = [], []
    for _ in range(MAX_TOOL_ROUNDS):
        msg = await _chat(messages, TOOLS)
        calls = msg.tool_calls or []
        if not calls:
            return {"reply": (msg.content or "").strip(), "actions": actions, "sources": sources}
        # Meta's docs: the full assistant message (with tool_calls) goes back
        # before the tool results, or the next call is a 400.
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name,
                                                      "arguments": c.function.arguments}} for c in calls]})
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except ValueError:
                args = {}
            name = c.function.name
            try:
                if (result := await _run_server_tool(db, user, name, args, "assistant", body.session_id)) is not None:
                    if name == "search_memory":
                        sources += [{"title": h["title"], "project": h["project"]} for h in result["hits"]]
                        if not result["hits"]:
                            result["note"] = "Nothing in memory."
                    elif name == "add_task":
                        actions.append({"type": "task_added", **result})  # shown in the chat, nothing to confirm
                elif (action := _device_action(name, args)):
                    actions.append(action)
                    result = {"status": "proposed; runs on the phone after the user confirms"}
                else:
                    result = {"error": f"unusable call to {name}"}
            except HTTPException as e:
                result = {"error": e.detail}
            messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(result)[:12000]})
    return {"reply": "I could not finish that; try asking more simply.", "actions": actions, "sources": sources}



# --- documents added to a class or meeting -------------------------------------------

def _extract_text(name: str, data: bytes) -> str:
    """Plain text of an upload. Runs in a thread: a big PDF takes seconds."""
    if name.lower().endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join((pg.extract_text() or "") for pg in reader.pages[:MAX_PDF_PAGES])
    else:
        text = data.decode("utf-8", errors="replace")
    return text.strip()[:MAX_FILE_TEXT]


def _file_out(f: SessionFile) -> dict:
    return {"id": f.id, "session_id": f.session_id, "name": f.name, "mime": f.mime, "size": f.size,
            "added_by": f.uploader_name, "created_at": f.created_at.isoformat()}


async def _add_file(db, s: ClassSession, m: ClassMember, user: AnswerUser,
                    name: str, data: bytes, text: str) -> SessionFile:
    ext = Path(name).suffix.lower()
    stem = re.sub(r"[^\w.-]+", "-", Path(name).stem).strip("-.")[:60] or "file"
    f = SessionFile(session_id=s.id, uploader_id=user.id, uploader_name=m.name,
                    name=f"{stem}{ext}", mime=FILE_TYPES[ext], size=len(data), memory_path="")
    db.add(f)
    await db.flush()
    f.memory_path = f"files/{f.id}-{stem}.md"
    folder = FILES_DIR / str(s.id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{f.id}{ext}").write_bytes(data)
    await db.commit()
    await memory.write(memory.session_project(s.id), f.memory_path,
                       f"# {f.name}\n\nAdded by {m.name} to the {s.kind} \"{s.title}\".\n\n{text or '(no text found)'}\n")
    return f


@router.post("/{session_id}/files", status_code=201)
async def upload_file(session_id: int, name: str, request: Request,
                      user: AnswerUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The file is the raw request body; `name` gives its type (.pdf, .txt, .md)."""
    s, m = await _member(db, session_id, user)
    ext = Path(name).suffix.lower()
    if ext not in FILE_TYPES:
        raise HTTPException(400, "Add a PDF, .txt or .md file")
    if int(request.headers.get("content-length") or 0) > MAX_FILE:
        raise HTTPException(413, "Files can be up to 10 MB")
    data = await request.body()
    if not data:
        raise HTTPException(400, "The file is empty")
    if len(data) > MAX_FILE:
        raise HTTPException(413, "Files can be up to 10 MB")
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        raise HTTPException(400, "That is not a PDF")
    try:
        text = await asyncio.to_thread(_extract_text, name, data)
    except Exception:  # noqa: BLE001  (a damaged PDF)
        raise HTTPException(400, "Could not read that file")
    f = await _add_file(db, s, m, user, name, data, text)
    return {**_file_out(f), "chars": len(text)}


@router.get("/{session_id}/files")
async def list_session_files(session_id: int, user: AnswerUser = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    s, _ = await _member(db, session_id, user)
    rows = (await db.execute(select(SessionFile).where(
        SessionFile.session_id == s.id).order_by(SessionFile.id))).scalars().all()
    return [_file_out(f) for f in rows]


@router.get("/{session_id}/files/{file_id}")
async def download_file(session_id: int, file_id: int, user: AnswerUser = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    s, _ = await _member(db, session_id, user)
    f = await db.get(SessionFile, file_id)
    if not f or f.session_id != s.id:
        raise HTTPException(404, "File not found")
    # Always a download, never rendered here: an uploaded file is someone else's content.
    return FileResponse(FILES_DIR / str(s.id) / f"{f.id}{Path(f.name).suffix}", media_type=f.mime,
                        filename=f.name, content_disposition_type="attachment")


# --- MCP tokens: what an AI app uses instead of the login key ------------------------

MCP_TOKEN_DAYS = 90
MCP_TOKENS_PER_PERSON = 10


def _token_hash(token: str) -> str:
    # ponytail: plain SHA-256 is enough for 256-bit random tokens; a slow hash
    # only matters for guessable secrets like passwords.
    return hashlib.sha256(token.encode()).hexdigest()


def _token_out(t: McpToken) -> dict:
    return {"id": t.id, "name": t.name, "can_write": t.can_write, "created_at": t.created_at.isoformat(),
            "expires_at": t.expires_at.isoformat(),
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None}


class NewMcpToken(BaseModel):
    name: str
    can_write: bool = False


@app.post("/api/mcp/tokens", status_code=201)
async def create_mcp_token(body: NewMcpToken, user: AnswerUser = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    """The token is in this response only; the server keeps its hash."""
    now = datetime.now(timezone.utc)
    active = (await db.execute(select(func.count()).select_from(McpToken).where(
        McpToken.person_id == user.id, McpToken.revoked_at.is_(None), McpToken.expires_at > now))).scalar()
    if active >= MCP_TOKENS_PER_PERSON:
        raise HTTPException(409, f"You have {MCP_TOKENS_PER_PERSON} tokens; revoke one first")
    token = "pcm_" + secrets.token_urlsafe(32)
    t = McpToken(person_id=user.id, name=_clean(body.name, 60, "Name"), token_hash=_token_hash(token),
                 can_write=body.can_write, expires_at=now + timedelta(days=MCP_TOKEN_DAYS))
    db.add(t)
    await db.commit()
    return {**_token_out(t), "token": token}


@app.get("/api/mcp/tokens")
async def my_mcp_tokens(user: AnswerUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(McpToken).where(
        McpToken.person_id == user.id, McpToken.revoked_at.is_(None), McpToken.expires_at > now)
        .order_by(McpToken.id.desc()))).scalars().all()
    return [_token_out(t) for t in rows]


@app.delete("/api/mcp/tokens/{token_id}")
async def revoke_mcp_token(token_id: int, user: AnswerUser = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    t = await db.get(McpToken, token_id)
    if not t or t.person_id != user.id:
        raise HTTPException(404, "Token not found")
    t.revoked_at = t.revoked_at or datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True}

# --- the plugin: memory over MCP for any local AI app (LM Studio, Claude, Muse Code) --
# Streamable HTTP, JSON responses only (modelcontextprotocol.io/specification/
# 2025-06-18/basic/transports). The bearer is one of the person's MCP tokens
# (never the login key), so a plugin sees exactly what that person sees, and
# writes only with a token allowed to; ai-memory's root token never leaves here.

MCP_ORIGINS = {o for o in os.getenv("MCP_ALLOWED_ORIGINS", "https://passaly.com").split(",") if o}
MCP_ONLY_TOOLS = {
    "list_files": {"description": "List the files you can read: recaps, transcripts, teacher answers "
                                  "and documents of your classes and meetings, and your own notes and "
                                  "profiles. Optionally only one class or meeting.",
                   "properties": {"session_id": {"type": "integer"}}, "required": []},
    "read_file": {"description": "Read one file in full by its uri (from list_files).",
                  "properties": {"uri": {"type": "string"}}, "required": ["uri"]},
    "add_document": {"description": "Add a markdown document to one of your classes or meetings; "
                                    "everyone in it can then read it, and the assistant uses it.",
                     "properties": {"session_id": {"type": "integer"}, "name": {"type": "string"},
                                    "text": {"type": "string"}},
                     "required": ["session_id", "name", "text"]},
}
MCP_TOOLS = [{"name": n, "description": t["description"],
              "inputSchema": {"type": "object", "properties": t["properties"], "required": t["required"]}}
             for n, t in {**SERVER_TOOLS, **MCP_ONLY_TOOLS}.items()]
MCP_WRITE_TOOLS = {"save_note", "add_document", "add_task", "update_task"}
URI_PREFIX = "passaly://"


async def _labels(db, user: AnswerUser) -> dict[str, str]:
    """project -> what to call it, for every project this person may read."""
    rows = (await db.execute(
        select(ClassSession).join(ClassMember, ClassMember.session_id == ClassSession.id)
        .where(ClassMember.user_id == user.id).order_by(ClassSession.id.desc()).limit(30))).scalars().all()
    return {memory.person_project(user.id): "Me",
            **{memory.session_project(s.id): f"{s.kind.capitalize()}: {s.title} ({s.created_at:%Y-%m-%d})"
               for s in rows}}


async def _list_files(db, user: AnswerUser, session_id: int | None = None) -> list[dict]:
    labels = await _labels(db, user)
    if session_id is not None:
        labels = {p: l for p, l in labels.items() if p == memory.session_project(session_id)}
    out = []
    for project, label in labels.items():
        for pg in await memory.listing(project):
            out.append({"uri": f"{URI_PREFIX}{project}/{pg['path']}", "name": pg["path"],
                        "title": f"{label} · {pg['title'] or pg['path']}", "mimeType": "text/markdown"})
    return out


async def _read_uri(db, user: AnswerUser, uri: str) -> dict | None:
    """The file behind a passaly:// uri, or None when it does not exist or is not yours."""
    if not uri.startswith(URI_PREFIX) or "/" not in uri[len(URI_PREFIX):]:
        return None
    project, path = uri[len(URI_PREFIX):].split("/", 1)
    if ".." in path or project not in await _labels(db, user):  # only your own projects
        return None
    return await memory.read(project, path)


def _aware(t: datetime) -> datetime:
    """SQLite hands datetimes back without their timezone; they were saved in UTC."""
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _rpc(id_, result=None, error=None):
    return JSONResponse({"jsonrpc": "2.0", "id": id_, **({"error": error} if error else {"result": result})})


@app.get("/mcp")
async def mcp_get():
    return Response(status_code=405)  # no server-initiated stream


@app.post("/mcp")
async def mcp(request: Request, db: AsyncSession = Depends(get_db)):
    origin = request.headers.get("origin")
    if origin and origin not in MCP_ORIGINS:  # the spec's DNS-rebinding guard
        raise HTTPException(403, "Origin not allowed")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    now = datetime.now(timezone.utc)
    t = token and (await db.execute(select(McpToken).where(
        McpToken.token_hash == _token_hash(token)))).scalar_one_or_none()
    if not t or t.revoked_at or _aware(t.expires_at) <= now:
        return JSONResponse({"error": "Create an MCP token in Passaly Classes and send it as the bearer"},
                            status_code=401, headers={"WWW-Authenticate": "Bearer"})
    p = await db.get(Person, int(t.person_id))
    user = AnswerUser(id=str(p.id), username=p.name, display_name=p.name)
    if not t.last_used_at or now - _aware(t.last_used_at) > timedelta(minutes=5):
        t.last_used_at = now  # ponytail: to the 5 minutes, not one write per request
        await db.commit()
    try:
        msg = await request.json()
    except ValueError:
        return _rpc(None, error={"code": -32700, "message": "Parse error"})
    if not isinstance(msg, dict):
        return _rpc(None, error={"code": -32600, "message": "One message per request"})
    if "id" not in msg:
        return Response(status_code=202)  # notification or response: accepted
    method, params, id_ = msg.get("method"), msg.get("params") or {}, msg["id"]
    if method == "initialize":
        return _rpc(id_, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                          "capabilities": {"tools": {}, "resources": {}},
                          "serverInfo": {"name": "passaly-memory", "version": "1.0"}})
    if method == "ping":
        return _rpc(id_, {})
    if method == "tools/list":
        return _rpc(id_, {"tools": [x for x in MCP_TOOLS if t.can_write or x["name"] not in MCP_WRITE_TOOLS]})
    if method == "resources/list":
        return _rpc(id_, {"resources": await _list_files(db, user)})
    if method == "resources/read":
        uri = str(params.get("uri", ""))
        page = await _read_uri(db, user, uri)
        if page is None:
            return _rpc(id_, error={"code": -32002, "message": "Resource not found", "data": {"uri": uri}})
        return _rpc(id_, {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": page["text"]}]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name in MCP_WRITE_TOOLS and not t.can_write:
            return _rpc(id_, {"content": [{"type": "text", "text": "This token is read-only."}], "isError": True})
        try:
            if (out := await _run_server_tool(db, user, name, args, "agent")) is not None:
                pass
            elif name == "list_files":
                sid = args.get("session_id")
                out = {"files": await _list_files(db, user, sid if isinstance(sid, int) else None)}
            elif name == "read_file":
                page = await _read_uri(db, user, str(args.get("uri", "")))
                if page is None:
                    raise HTTPException(404, "No such file, or it is not yours")
                out = page
            elif name == "add_document":
                sid = args.get("session_id")
                if not isinstance(sid, int):
                    raise HTTPException(400, "session_id is a number from list_files")
                s, m = await _member(db, sid, user)
                doc_name = _clean(str(args.get("name", "")), 100, "Name")
                if not doc_name.lower().endswith(".md"):
                    doc_name += ".md"
                text = _clean_block(str(args.get("text", "")), MAX_FILE_TEXT, "Text")
                f = await _add_file(db, s, m, user, doc_name, text.encode(), text)
                out = {**_file_out(f), "uri": f"{URI_PREFIX}{memory.session_project(s.id)}/{f.memory_path}"}
            else:
                return _rpc(id_, error={"code": -32602, "message": f"Unknown tool {name}"})
        except HTTPException as e:
            return _rpc(id_, {"content": [{"type": "text", "text": str(e.detail)}], "isError": True})
        return _rpc(id_, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                          "isError": False})
    return _rpc(id_, error={"code": -32601, "message": f"Method not found: {method}"})

app.include_router(router)

# Llama model files for the phones, served from the laptop on the same Wi-Fi.
# Meta's license travels with them: LICENSE.txt and NOTICE.txt sit in the folder.
MODELS = Path(os.getenv("MODELS_DIR", Path(__file__).parent / "models"))
if MODELS.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/models", StaticFiles(directory=MODELS), name="models")

STATIC = Path(__file__).parent / "static"


@app.get("/{path:path}", include_in_schema=False)
async def page(path: str):
    """One page app: every non-API path gets index.html (invite links included)."""
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(STATIC / "index.html")
