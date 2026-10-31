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
    return _engine


def get_session_factory(database_url: str) -> sessionmaker:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = sessionmaker(
            get_engine(database_url), class_=AsyncSession, expire_on_commit=False
        )
    return _async_session_factory


async def create_tables(database_url: str):
    async with get_engine(database_url).begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
