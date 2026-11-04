# Passaly Classes

**Live classes and meetings → a recap → a tutor and an assistant that remember
them.** The AI runs in Meta's cloud (Muse Spark) or entirely on the phone
(Llama 3.2), sharing one memory. Any AI app can read that memory through an
MCP server.

Built for a hackathon. **[ABOUT.md](ABOUT.md)** explains it in plain words;
this file is for building and running it. Live at **https://passaly.com/classes**.

![How a class or meeting flows](docs/flow.svg)

---

## Architecture

![Architecture](docs/architecture.svg)

| Part | What it is | Where |
|---|---|---|
| Web page | Classes and meetings in the browser, live captions (Web Speech API), files, MCP tokens | `static/index.html` |
| Android app | Captions (SpeechRecognizer), tutor, assistant with tasks, Llama 3.2 on the phone (LiteRT-LM), files, MCP tokens | `app/` (Flutter) |
| Class server | Classes, transcript, tutor and teacher inbox, recaps, profiles, assistant, files, MCP server and tokens | `app.py`, `models.py` (FastAPI, SQLAlchemy, SQLite) |
| Shared memory | One space per class/meeting and per person: transcripts, recaps, answers, file text, notes, profiles | `memory.py` → [ai-memory](https://github.com/akitaonrails/ai-memory) |
| Cloud brain | Recaps, profiles, tutor, assistant (tool calling) | Meta Model API, `muse-spark-1.3` |
| Phone brain | The same jobs, on the device | Llama 3.2 1B / 3B (`models/index.json`) |

---

## Run it locally

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/uvicorn app:app --reload --port 8010      # http://localhost:8010
    .venv/bin/python test_app.py                         # the whole flow, fake AI

Live captions need Chrome or Edge; other browsers can type lines. With no
settings at all it runs without cloud AI and without shared memory: classes,
captions, the inbox and files all work, and the phone app does the AI.

| Setting | Default | What for |
|---|---|---|
| `TUTOR_API_KEY` | none (no cloud AI) | Key for any OpenAI-compatible API: Meta Model API for Muse |
| `TUTOR_BASE_URL` | OpenAI | `https://api.meta.ai/v1` for Muse |
| `TUTOR_MODEL` | `gpt-4o-mini` | `muse-spark-1.3` |
| `AI_MEMORY_URL` | none (memory off) | ai-memory server, e.g. `http://ai-memory:49374` |
| `AI_MEMORY_AUTH_TOKEN` | none | its bearer token |
| `AI_MEMORY_WORKSPACE` | `passaly-classes` | ai-memory workspace name |
| `DATABASE_URL` | `sqlite+aiosqlite:///classes.db` | the database |
| `FILES_DIR` | `./files` | uploaded originals |
| `MODELS_DIR` | `./models` | Llama files, when this server serves them itself |
| `MCP_ALLOWED_ORIGINS` | `https://passaly.com` | browser origins allowed on `/mcp` |

Using Muse: the Meta Model API is OpenAI-compatible
([docs](https://dev.meta.ai/docs/overview)), so the three `TUTOR_` settings are
all it takes. Muse is sent no `temperature`; Meta says it is tuned for the default
([docs](https://dev.meta.ai/docs/protocols/chat-completions)).

---

## The live deployment (VPS)

Two containers on the VPS's Docker network `answer-net`, behind its nginx:

| Container | Image | Exposed |
|---|---|---|
| `classes` | built from this repo's `Dockerfile` | through nginx at `/classes/` only |
| `ai-memory` | `akitaonrails/ai-memory:3.2.10` | nothing: the Docker network only |

On the server, `~/class_tutor` holds the code and three private files (mode
600, never in git): `meta.env` (Muse settings and key), `aimem.env`
(ai-memory's token), `classes-mem.env` (address and token for the class
server). Data lives in the volumes `classes_data` (SQLite and uploads) and
`ai-memory-data`.

Redeploy after a change:

    rsync -a --exclude .venv --exclude app --exclude .git --exclude files \
      --exclude '*.db' --exclude '*.env' ./ ubuntu@<vps>:class_tutor/
    ssh ubuntu@<vps> 'cd ~/class_tutor && docker build -q -t class-tutor . &&
      docker rm -f classes && docker run -d --name classes --network answer-net \
      --restart unless-stopped -v classes_data:/data \
      --env-file meta.env --env-file classes-mem.env class-tutor'

Helper scripts in the server's home folder:

- `./set-meta-key.sh` asks for the Meta Model API key (hidden), tests it with
  one tiny request, saves it in `meta.env` and restarts `classes`.
- `./fetch-llama.sh` asks for a Hugging Face token (hidden; Meta's license
  must be accepted on the model pages first), downloads both Llama files to
  `/classes/models/`, and keeps a file only if its SHA-256 matches `index.json`.

nginx: `/classes/` proxies to `classes:8010` (resolved per request, so nginx
still starts if the container is down), with its own `Permissions-Policy`
that allows the microphone for captions; `/classes/models/` is served from
disk.

---

## The phone app (Android)

    cd app
    flutter test
    flutter build apk --release          # build/app/outputs/flutter-apk/app-release.apk

On first start the app asks for a name and the class server, which is already
filled in with `passaly.com/classes`. A domain is reached over HTTPS and an IP
like `192.168.1.76:8010` over plain HTTP, for a laptop on the same Wi-Fi.

**Tutor AI** (chip icon): choose **This phone · Llama** or **Meta cloud · Muse**,
and download Llama: the 1B (~0.96 GB) or the 3B (~2.2 GB, better at tasks). The
app checks each file against the SHA-256 in `models/index.json` and greys out
models the phone's RAM cannot hold.

<details><summary>Running the server on a laptop for the phones (WSL 2)</summary>

Start it with `--host 0.0.0.0`. WSL 2 is not reachable from the Wi-Fi by
default ([Microsoft docs](https://learn.microsoft.com/en-us/windows/wsl/networking)).
In an admin PowerShell (redo the first line after a reboot):

    netsh interface portproxy add v4tov4 listenport=8010 listenaddress=0.0.0.0 connectport=8010 connectaddress=$(wsl hostname -I).Trim()
    New-NetFirewallRule -DisplayName "Passaly Classes 8010" -Direction Inbound -Protocol TCP -LocalPort 8010 -Action Allow

Put the Llama files and `LICENSE.txt` / `NOTICE.txt` in `models/`.
</details>

---

## How the pieces work

**Classes and meetings.** A session is a `class` or a `meeting`. In a
meeting, teacher/student read as host/participant. Each device posts only
its own speaker's caption lines.

**After it ends.** With Muse, the server writes the recap (summary, decisions,
action items, open questions) and the profiles, then saves the transcript and
recap to memory. With Llama, the host's phone writes the recap and sends it
(`PUT /api/classes/{id}/recap`), and each phone writes its own profile.

**Tutor.** A student's question is answered only from the class's transcript
and the teacher's later answers. If they don't cover it, the question goes to
the teacher's inbox, and her answer joins the class material.

**Assistant.** It answers from the shared memory and proposes tasks:
`add_calendar_event`, `share_text`, `save_note`.
- Muse: `POST /api/assistant`, with Meta's tool calling ([docs](https://dev.meta.ai/docs/tool-calling)).
- Llama: the phone reads `GET /api/memory/search` and writes `ACTION {...}` lines.

Both paths check the arguments the same way. Calendar and share open the
phone's own app, filled in ([Android intents](https://developer.android.com/guide/components/intents-common#AddEvent)),
and run only when the user taps.

**Files.** Anyone in a session can add a PDF, .txt or .md (10 MB). The
original is kept for download; its text (`pypdf` for PDFs) goes into the
session's memory, so the assistant and MCP can use it.

**Memory.** [ai-memory](https://github.com/akitaonrails/ai-memory) over its
MCP endpoint (`memory_write_page`, `memory_query`, `memory_read_page`,
`memory_recent`). Spaces are `session-<id>` and `person-<id>`; a search only
covers the asker's own spaces. A search with no keyword match falls back to
the newest recaps.

---

## The MCP server

`https://passaly.com/classes/mcp` speaks MCP over Streamable HTTP with JSON
responses ([spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)).
It hosts each person's files and nothing of anyone else's.

| Tool | Does | Needs write |
|---|---|---|
| `list_files` | Every file you can read, optionally for one class or meeting, as `passaly://` URIs | |
| `read_file` | One file in full | |
| `search_memory` | Keyword search over all of it | |
| `save_note` | A note in your own memory | ✔ |
| `add_document` | A markdown document in one of your classes or meetings | ✔ |

The same files are MCP **resources** (`resources/list`, `resources/read`;
[spec](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)).

**Tokens.** Create one per AI app in the web page ("Connect an AI app") or
the app (menu → Connect an AI app). A token:

- is separate from the login key, which `/mcp` refuses;
- is shown once and stored only as its SHA-256;
- is read-only unless writing was allowed (read-only tokens don't even list
  the write tools);
- can be revoked at any time, and expires after 90 days (10 per person).

Client config, e.g.:

    {"mcpServers": {"passaly": {"url": "https://passaly.com/classes/mcp",
      "headers": {"Authorization": "Bearer pcm_<your token>"}}}}

For a Claude custom connector, put the header under *Request headers*
([Claude help](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)).

---

## API

All `/api` calls send `X-Person-Key` (from `POST /api/people`).
`/mcp` takes an MCP token instead.

| Method | Path | What |
|---|---|---|
| POST | `/api/people` | Sign in with a name → key |
| POST · GET | `/api/classes` | Start a class or meeting · my sessions |
| POST | `/api/classes/join/{token}` | Join with an invite |
| GET | `/api/classes/{id}` | One session, its people and recap |
| POST · GET | `/api/classes/{id}/lines` | Add a caption line · new lines since `after` |
| POST | `/api/classes/{id}/end` | End it (host); starts recap and profiles |
| PUT | `/api/classes/{id}/recap` | Recap written on the host's phone |
| POST | `/api/classes/{id}/ask` | Tutor question, answered by Muse |
| POST · GET | `/api/classes/{id}/questions` | A question the phone answered · questions |
| GET | `/api/classes/inbox/waiting` | The teacher's inbox |
| POST | `/api/classes/questions/{id}/answer` | The teacher answers |
| GET | `/api/classes/{id}/pack` | Everything the phone's tutor needs |
| PUT | `/api/profiles/{kind}` | A profile written on the phone |
| POST · GET | `/api/classes/{id}/files` | Upload (raw body, `?name=`) · list |
| GET | `/api/classes/{id}/files/{fid}` | Download the original |
| GET | `/api/memory/search?q=` | Memory pages for the phone's assistant |
| POST | `/api/memory/notes` | Save a note |
| POST | `/api/assistant` | Assistant with Muse: reply + proposed tasks |
| GET | `/api/tutor` | Is a cloud model set up? Which? |
| POST · GET · DELETE | `/api/mcp/tokens` | Create · list · revoke MCP tokens |
| POST | `/mcp` | The MCP server |

---

## Tests

    .venv/bin/python test_app.py     # server: the class flow, meetings, recaps, memory
                                     # privacy, assistant tools, files (a real PDF),
                                     # MCP and its tokens, the SQLite lock regression
    cd app && flutter test           # app: retrieval, replies, ACTION parsing, time zones

## Built with Llama

Llama 3.2 is licensed under the Llama 3.2 Community License, Copyright © Meta
Platforms, Inc. All Rights Reserved. The app shows this, and the license text,
on its "Tutor AI" screen.

## Moving into Passaly later

Same stack and style as quiz-api (FastAPI, SQLAlchemy async, one router).
Move the router and the tables into quiz-api, swap `get_person` for
`auth.get_current_user`, drop the `Person` table, and point `DATABASE_URL`
at Postgres. The page becomes a React route.
