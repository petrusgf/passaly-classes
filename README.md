# Passaly Classes (hackathon)

Live classes with a personal AI tutor afterwards. A teacher opens a class and
shares the invite link; during the call (Meet, Zoom, anything) each browser
turns its own speaker's voice into text, so the transcript is labeled by who
spoke. No audio or video is stored. When the teacher ends the class, the AI
updates two profiles: how this teacher teaches and how each student learns.
After class the student asks the tutor. It answers from the transcript in the
teacher's style, and anything the class did not cover goes to the teacher's
inbox. Her answer reaches the student and joins that class's material.

## Run

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    export TUTOR_API_KEY=...          # any OpenAI-compatible endpoint
    # export TUTOR_BASE_URL=...       # e.g. Google's OpenAI-compatible API, or Ollama
    # export TUTOR_MODEL=...          # default gpt-4o-mini
    .venv/bin/uvicorn app:app --reload --port 8010   # http://localhost:8010

Live captions need Chrome or Edge (Web Speech API); other browsers can type lines.

    .venv/bin/python test_app.py      # whole flow with a fake AI

## Moving into Passaly later

Same stack and style as quiz-api (FastAPI, SQLAlchemy async, one router).
Move the router and the class tables into quiz-api, swap `get_person` for
`auth.get_current_user`, drop the `Person` table, and point `DATABASE_URL` at
Postgres. The page becomes a React route.
