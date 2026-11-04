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

import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import memory
from models import (
    ClassLine, ClassMember, ClassSession, LearnerProfile, Person, TutorQuestion,
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
           "role": role, "kind": s.kind, "recap": s.recap,
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
    s = ClassSession(teacher_id=user.id, teacher_name=_name(user),
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
        tasks.add_task(_update_profiles, s.id)
    return _session_out(s, m.role)


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
    return {"cloud": bool(TUTOR_API_KEY), "model": TUTOR_MODEL if TUTOR_API_KEY else None}


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
    query = _clean(query, 300, "Search")
    return await memory.search(await _my_projects(db, user, session_id), query)


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


# --- the assistant: Muse with tools; the phone runs the device ones -----------------

# Run here: they touch the shared memory.
SERVER_TOOLS = {
    "search_memory": {"description": "Search the user's classes, meetings, recaps and notes.",
                      "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "save_note": {"description": "Save a note to the user's memory.",
                  "properties": {"text": {"type": "string"}}, "required": ["text"]},
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
    "When the user wants something done, call add_calendar_event, share_text or "
    "save_note. Calendar and share run on the user's phone after they confirm, so "
    "say what you proposed. Reply briefly, in the user's language."
)
MAX_TOOL_ROUNDS = 4
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
    messages = [{"role": "system", "content": ASSISTANT_SYSTEM.format(
        name=_name(user), now=(body.now or datetime.now(timezone.utc).isoformat())[:40])}]
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
                if name == "search_memory":
                    hits = await _search_memory(db, user, str(args.get("query", "")), body.session_id)
                    sources += [{"title": h["title"], "project": h["project"]} for h in hits]
                    result = {"hits": hits} if hits else {"hits": [], "note": "Nothing in memory."}
                elif name == "save_note":
                    result = await _save_note(user, str(args.get("text", "")))
                elif (action := _device_action(name, args)):
                    actions.append(action)
                    result = {"status": "proposed; runs on the phone after the user confirms"}
                else:
                    result = {"error": f"unusable call to {name}"}
            except HTTPException as e:
                result = {"error": e.detail}
            messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(result)[:12000]})
    return {"reply": "I could not finish that; try asking more simply.", "actions": actions, "sources": sources}


# --- the plugin: memory over MCP for any local AI app (LM Studio, Claude, Muse Code) --
# Streamable HTTP, JSON responses only (modelcontextprotocol.io/specification/
# 2025-06-18/basic/transports). The bearer is the person's own key, so a plugin
# sees exactly what that person sees; ai-memory's root token never leaves here.

MCP_ORIGINS = {o for o in os.getenv("MCP_ALLOWED_ORIGINS", "https://passaly.com").split(",") if o}
MCP_TOOLS = [{"name": n, "description": t["description"],
              "inputSchema": {"type": "object", "properties": t["properties"], "required": t["required"]}}
             for n, t in SERVER_TOOLS.items()]


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
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    p = key and (await db.execute(select(Person).where(Person.key == key))).scalar_one_or_none()
    if not p:
        return JSONResponse({"error": "Use your Passaly Classes key as the bearer token"}, status_code=401)
    user = AnswerUser(id=str(p.id), username=p.name, display_name=p.name)
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
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "passaly-memory", "version": "1.0"}})
    if method == "ping":
        return _rpc(id_, {})
    if method == "tools/list":
        return _rpc(id_, {"tools": MCP_TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            if name == "search_memory":
                out = {"hits": await _search_memory(db, user, str(args.get("query", "")))}
            elif name == "save_note":
                out = await _save_note(user, str(args.get("text", "")))
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
