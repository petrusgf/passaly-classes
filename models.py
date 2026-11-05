"""
SQLAlchemy async models for live classes and the class tutor.

Written in quiz-api's style so the tables can move into Passaly as they are:
there, Person goes away and user ids come from Answer.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

# SQLite auto-numbers only INTEGER primary keys; Postgres (Passaly) keeps BIGINT.
ID = BigInteger().with_variant(Integer, "sqlite")


def _now():
    return datetime.now(timezone.utc)


class Person(Base):
    """Demo stand-in for a Passaly account: a name and a secret key the
    browser keeps. ponytail: no passwords or email; Answer's login replaces it."""
    __tablename__ = "person"

    id = Column(ID, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    key = Column(Text, nullable=False, unique=True)
    # Signed in with Google: the account's permanent id ("sub"), and the
    # refresh token that lets the server create Meet links and read their
    # transcripts. ponytail: stored as is on the private /data volume.
    google_sub = Column(Text, index=True)
    google_refresh = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


# A teacher holds a class over any video call; each browser turns its own
# speaker's voice into text and posts the lines here. No audio or video is kept.
# After class the transcript, the teacher's later answers and two profiles are
# the class's "pack" for the tutor.


class ClassSession(Base):
    __tablename__ = "class_session"

    id = Column(ID, primary_key=True, autoincrement=True)
    teacher_id = Column(Text, nullable=False, index=True)
    teacher_name = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    subject = Column(Text)
    # BCP 47 tag the browsers transcribe in ("en-US", "pt-BR").
    lang = Column(Text, nullable=False, default="en-US")
    # Meet/Zoom link: the call itself happens there.
    call_url = Column(Text)
    invite_token = Column(Text, nullable=False, unique=True)
    # live while lines are accepted; ended once the teacher closes it.
    status = Column(Text, nullable=False, default="live")
    # class | meeting. ponytail: a meeting is a class whose roles read
    # host / participant on screen; the columns keep teacher/student.
    kind = Column(Text, nullable=False, default="class")
    # After it ends: summary, decisions, action items, open questions (markdown).
    recap = Column(Text)
    # A Google Meet space this app created ("spaces/abc"); its transcript
    # becomes the lines after the meeting.
    meet_space = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class ClassMember(Base):
    __tablename__ = "class_member"
    __table_args__ = (UniqueConstraint("session_id", "user_id"),)

    id = Column(ID, primary_key=True, autoincrement=True)
    session_id = Column(
        ID, ForeignKey("class_session.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(Text, nullable=False, index=True)
    name = Column(Text, nullable=False)
    role = Column(Text, nullable=False)  # teacher | student


class ClassLine(Base):
    __tablename__ = "class_line"

    id = Column(ID, primary_key=True, autoincrement=True)
    session_id = Column(
        ID, ForeignKey("class_session.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    role = Column(Text, nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class TutorQuestion(Base):
    __tablename__ = "tutor_question"

    id = Column(ID, primary_key=True, autoincrement=True)
    session_id = Column(
        ID, ForeignKey("class_session.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    student_id = Column(Text, nullable=False, index=True)
    student_name = Column(Text, nullable=False)
    question = Column(Text, nullable=False)
    ai_answer = Column(Text)
    # False: the class did not cover it, so it waits for the teacher.
    from_class = Column(Boolean, nullable=False, default=True)
    teacher_answer = Column(Text)
    status = Column(Text, nullable=False)  # answered | waiting_teacher | teacher_answered
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class Task(Base):
    """Something to do, from a class or meeting (or just the person's own):
    added in the app, by the assistant, or by an agent over MCP. Agents read
    it from memory (tasks/<id>.md), do it, and report back with a result."""
    __tablename__ = "task"

    id = Column(ID, primary_key=True, autoincrement=True)
    session_id = Column(
        ID, ForeignKey("class_session.id", ondelete="CASCADE"), index=True,
    )  # None: a personal task, seen only by its owner
    owner_id = Column(Text, nullable=False, index=True)
    owner_name = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    details = Column(Text)
    assignee = Column(Text)  # free text: "Ana", "Claude Code", …
    due = Column(Text)  # as said: "Friday", "2026-11-07"
    status = Column(Text, nullable=False, default="open")  # open | in_progress | done
    result = Column(Text)  # what whoever did it reports back
    source = Column(Text, nullable=False, default="app")  # app | assistant | agent
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class SessionFile(Base):
    """A document someone added to a class or meeting. The original stays on
    disk for download; its text goes into the session's shared memory."""
    __tablename__ = "session_file"

    id = Column(ID, primary_key=True, autoincrement=True)
    session_id = Column(
        ID, ForeignKey("class_session.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    uploader_id = Column(Text, nullable=False)
    uploader_name = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    mime = Column(Text, nullable=False)
    size = Column(Integer, nullable=False)
    memory_path = Column(Text, nullable=False)  # the page in ai-memory
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class McpToken(Base):
    """A token for the MCP plugin, separate from the login key: shown once,
    kept only as its SHA-256, read-only unless allowed, revocable, expiring."""
    __tablename__ = "mcp_token"

    id = Column(ID, primary_key=True, autoincrement=True)
    person_id = Column(Text, nullable=False, index=True)
    name = Column(Text, nullable=False)
    token_hash = Column(Text, nullable=False, unique=True)
    can_write = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_used_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))


class LearnerProfile(Base):
    """What the AI learned about one person: a teacher's way of teaching, or a
    student's way of learning. Rewritten as a whole after each class."""
    __tablename__ = "learner_profile"
    __table_args__ = (UniqueConstraint("user_id", "kind"),)

    id = Column(ID, primary_key=True, autoincrement=True)
    user_id = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)  # teacher | student
    text = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)


_engine = None
_async_session_factory = None


def get_engine(database_url: str):
    global _engine
    if _engine is None:
        _engine = create_async_engine(database_url, echo=False)
        if database_url.startswith("sqlite"):
            # WAL: readers and the writer stop blocking each other; busy_timeout:
            # a second writer waits its turn instead of failing at once.
            @event.listens_for(_engine.sync_engine, "connect")
            def _pragmas(conn, _):
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
    return _engine


def get_session_factory(database_url: str) -> sessionmaker:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = sessionmaker(
            get_engine(database_url), class_=AsyncSession, expire_on_commit=False
        )
    return _async_session_factory


# Columns added after the first deploy: create_all makes new tables only, so
# an existing database gets these with ALTER TABLE.
_ADDED = {"class_session": {"kind": "TEXT NOT NULL DEFAULT 'class'", "recap": "TEXT", "meet_space": "TEXT"},
          "person": {"google_sub": "TEXT", "google_refresh": "TEXT"}}


async def create_tables(database_url: str):
    async with get_engine(database_url).begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table, cols in _ADDED.items():
            have = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns(table)})
            for name, ddl in cols.items():
                if name not in have:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
