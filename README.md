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

## Phone app (Android): the tutor runs on the phone with Llama

`app/` is a Flutter app. All AI runs on the phone with Meta's Llama 3.2
(1B or 3B, picked per phone RAM) through LiteRT-LM, the runtime the Passaly app
already uses for Gemma. The server only stores classes, lines, questions and
profiles; with the phone app, no cloud AI sees a student's questions.

- Captions: Android's own speech recognizer, offline when the phone has the
  language pack.
- Tutor: the phone first matches the question against the transcript. No
  match goes straight to the teacher; a match is answered by Llama from only
  those passages, in the teacher's style.
- Profiles: the teacher's phone writes her teaching style when she ends the
  class; each student's phone writes their learning profile once afterwards.

### 1. Put the Llama files on the laptop

Accept Meta's license on Hugging Face (litert-community/Llama-3.2-1B and
-3B), then download into `models/` next to `models/index.json`:

    models/llama3_2_1b_mixed_int4_gpu.litertlm
    models/llama3_2_3b_mixed_int4_gpu.litertlm   (optional)
    models/LICENSE.txt  models/NOTICE.txt        (from the same repo; the license
                                                  requires shipping them)

The app checks each file against the SHA-256 in `index.json`.

### 2. Let the phones reach the laptop

The server must listen on all interfaces:

    .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8010

WSL 2 is not reachable from the Wi-Fi by default
([Microsoft docs](https://learn.microsoft.com/en-us/windows/wsl/networking)).
In an admin PowerShell on Windows (redo the first line after a reboot; the
WSL address changes):

    netsh interface portproxy add v4tov4 listenport=8010 listenaddress=0.0.0.0 connectport=8010 connectaddress=$(wsl hostname -I).Trim()
    New-NetFirewallRule -DisplayName "Passaly Classes 8010" -Direction Inbound -Protocol TCP -LocalPort 8010 -Action Allow

In the app, the class server is the laptop's Wi-Fi address, e.g. `192.168.1.76:8010`.

### 3. Build and install

    cd app && flutter build apk --release
    adb install build/app/outputs/flutter-apk/app-release.apk

`flutter test` runs the tutor's retrieval and reply checks.

## Meetings, the assistant and shared memory

A session is a **class** (tutor for students afterwards) or a **meeting**
(host and participants). When either ends, the AI writes a recap: summary,
decisions, action items, open questions. Muse does it on the server; with
Llama, the host's phone writes it and sends it with `PUT /api/classes/{id}/recap`.

Everything said, recapped and answered goes into **ai-memory**
([akitaonrails/ai-memory](https://github.com/akitaonrails/ai-memory)), one
project per session and per person, so a search only sees what the asker
belongs to. On the VPS it runs as the private container `ai-memory`
(no public port; the class server reaches it on the Docker network with the
token in `~/class_tutor/aimem.env`).

The **assistant** (phone app, ✨ button) answers from that memory and proposes
tasks: calendar event, share text, save note. The same tasks for both brains:

- Meta cloud: `POST /api/assistant`, Muse Spark with tool calling.
- This phone: Llama reads `GET /api/memory/search` and writes `ACTION {...}` lines.

Calendar and share open the phone's own app, filled in, for the user to confirm.

**Files**: anyone in a class or meeting can add a PDF, .txt or .md (up to
10 MB) from the web page or the app. The original is kept for download
(`/data/files` in the container); its text goes into the session's memory,
so the tutor, the assistant and the MCP plugin can use it.

### The plugin: memory in any local AI app (MCP)

`https://passaly.com/classes/mcp` is an MCP server (Streamable HTTP, JSON
responses) that hosts each person's files: recaps, transcripts, teacher
answers, documents added to their classes and meetings, notes and profiles.

| Tool | Does |
|---|---|
| `list_files` | Every file you can read (optionally one class or meeting), as `passaly://` URIs |
| `read_file` | One file in full |
| `search_memory` | Keyword search over all of it |
| `save_note` | A note in your own memory |
| `add_document` | A markdown document into one of your classes or meetings |

The same files are MCP **resources** (`resources/list`, `resources/read`).
**Authentication: MCP tokens.** Create one per AI app on the web page
("Connect an AI app") or in the app (menu → Connect an AI app). A token is:

- separate from the login key (which `/mcp` refuses),
- shown once and stored only as its SHA-256,
- read-only unless "allow it to add notes and documents" was ticked
  (read-only tokens do not even list the write tools),
- revocable at any time, and expires after 90 days.

It sees exactly what its owner sees in the app, nothing of anyone else's.
Cloud agents work too (Claude custom connectors: put the header under
*Request headers*). For example, in an MCP client config:

    {"mcpServers": {"passaly": {"url": "https://passaly.com/classes/mcp",
      "headers": {"Authorization": "Bearer pcm_<your token>"}}}}

## Built with Llama

Llama 3.2 is licensed under the Llama 3.2 Community License, Copyright © Meta
Platforms, Inc. All Rights Reserved. The app shows this and the license text
on its "Llama on this phone" screen.

## Moving into Passaly later

Same stack and style as quiz-api (FastAPI, SQLAlchemy async, one router).
Move the router and the class tables into quiz-api, swap `get_person` for
`auth.get_current_user`, drop the `Person` table, and point `DATABASE_URL` at
Postgres. The page becomes a React route.
