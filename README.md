# Strux

*(formerly Watcher AI — the Python package is still called `watcher`)*

A fully local, fully private desktop "watcher + suggester" for Windows 11.
It quietly records what you do (active window, browser URL, idle, clipboard,
screenshots with on-device OCR), runs cheap rules every 30 s, asks a small
local LLM (Ollama) every few minutes for one suggestion, shows it on a small
always-on-top overlay card, and gives you a dashboard to see your day, search
it, and ask questions about it. Nothing leaves the machine.

`ARCHITECTURE.md` has the design, performance budget, screenshot keep/drop
policy and the work log.

## Starting it (no terminal needed)

Already set up on this PC by `install-autostart.ps1`:

- **Starts at logon** automatically (Task Scheduler task `Strux`, hidden in the tray).
- **Start Menu / Desktop shortcut** "Strux": opens the dashboard. If it is
  already running, the shortcut just brings the window to the front.
- **Tray icon** (violet ring): Open dashboard · Pause · Think now · Forget last 10 min · Quit.

Hotkeys (global):

| Keys | Action |
|---|---|
| `Ctrl+Alt+Space` | overlay: quick chat with the model about your activity |
| `Ctrl+Alt+W` | show / hide the dashboard window |
| `Ctrl+Alt+P` | pause / resume watching |
| `Ctrl+Alt+F` | forget the last 10 minutes (rows + files) |
| `Esc` (in overlay) | close the overlay |

From a terminal (dev): `.\run.ps1`, or `.\.venv\Scripts\python -m watcher run [--hidden] [--no-window]`.

## The bar

A slim white strip at the top edge of the screen, drawn natively (true transparency,
smooth spring animations, clicks pass through everything around it).

| Do this | Get this |
|---|---|
| look at it | status dot · what you are doing or the latest item · count |
| hover | it widens: logo, headline, chat · activity · dashboard · pause · think |
| a suggestion / alert / Claude request arrives | it drops out of the bar with accept / snooze / dismiss, tucks back after ~14 s |
| click the keyboard icon (or `Ctrl+Alt+Space`) | the chat drop-down opens under the bar |
| click the list icon or the headline | the activity drop-down (suggestions, alerts, Claude sessions, busy processes) |
| drag it | slides anywhere along the top of any monitor; position is remembered |
| double-click it | back to the centre |
| right-click it | opens the dashboard |

`Esc` or clicking elsewhere closes the drop-down. In it, the tabs and input stay put
and only the list scrolls.

## Background awareness

- **Processes** (`sensors.process`): starts, exits, "busy" (>25 % CPU for 3 polls)
  and `job_done` when a build/test/install style process (node, npm, gradle,
  msbuild, cargo, python, pytest, dotnet, tsc, docker, pyinstaller, …) exits
  after running ≥ 20 s. A `job_done` becomes a suggestion unless you are
  already looking at a terminal or editor. The dashboard shows the top CPU users.
- **Claude Code** (hooks → `hooks/claude_hook.py` → `POST /api/ingest/claude`):
  prompts, tool calls, permission requests, finishes and session ends are
  recorded as `claude` events (one-line summaries only, never full inputs or
  transcripts). "Claude needs your approval" and "Claude finished" pop out of
  the bar. Installed on this PC in `~/.claude/settings.json`; use
  `install-claude-hooks.ps1` (`-Remove` to undo) on another machine.

## Local models

Installed on this PC via Ollama:

| Model | Used for | Size |
|---|---|---|
| `qwen2.5:7b-instruct-q4_K_M` | suggestions, Ask, overlay chat | 4.7 GB |
| `qwen2.5vl:3b` | "Explain this screen" | 3.2 GB |

Background suggestions unload the model right after each answer (`keep_alive: 0`)
so VRAM stays free for your work. Interactive chat keeps it warm for 3 minutes
(`chat_keep_alive`). First answer after an unload costs the ~15 s model load.

## Dashboard

Black / white / blue palette, light only. Keyboard:
`/` search, `g d` / `g t` / `g s` / `g a` / `g c` jump to pages.

| Page | What it does |
|---|---|
| Dashboard | greeting + what you are doing now; Phosphor icons, light/dark; active time, sessions, screens, suggestions; 24 h activity curve; time per app; **Claude Code** sessions; **Background** top processes and recent jobs; latest suggestions; live feed |
| Timeline | proportional strip, per-app filter, sessions with URLs / copies, screenshot grid, click to open + "Explain this screen" |
| Chat attachments | paperclip / paste / drop: screenshots and photos go to the vision model, text / code / PDF / Word files are read into the chat |
| Memory | blocks of context about you (about me, preferences, focus, notes) that every AI feature reads; edit them, see exactly what the AI gets, or tell the chat “remember that …” |
| TODOs | quick add with due / priority / project syntax, grouped by due date; the chat and suggestions add here too |
| Calendar | month grid + day agenda + event editor; the chat can add, move and read events |
| Suggestions | the accept button runs the suggestion (Explain an error, Recap after a break, Start focus / break timer, Ask); snooze brings it back in 30 min; dismissals make that rule quieter |
| Search | full-text over titles, URLs, clipboard previews and OCR text |
| Ask | chat with the local model about your own activity (warns if Ollama / the model is missing) |
| Settings | blocklists, screenshot/OCR/retention knobs, model, rules, hotkeys; saved to `config.yaml` and hot-applied |

## Dev setup

```powershell
py -V:Astral/CPython3.12.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt -r requirements-ocr.txt -r requirements-dev.txt
cd ui; npm install; npm run build; cd ..
.\.venv\Scripts\python -m unittest discover -s tests -v     # 23 tests
```

## CLI

```
python -m watcher run [--hidden] [--no-window]   start (default); second launch just shows the running window
python -m watcher open                           open the dashboard URL in a browser
python -m watcher timeline --minutes 60 -v
python -m watcher stats | suggestions | feedback <id> accept|dismiss|snooze | retention | llm-test

Demo the welcome-back flow: `Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/debug/welcome -Body '{"minutes":25}' -ContentType application/json`

Demo a suggestion in the bar: `Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/suggestions/test -Body '{"text":"hello"}' -ContentType application/json`
```

## Packaging and autostart

```powershell
.\build.ps1                   # dist\Strux\Strux.exe (portable folder, PyInstaller, own icon)
.\install-autostart.ps1       # logon task + Start Menu/Desktop shortcuts;  -Remove to undo
```

The packaged exe keeps its data and an editable `config.yaml` in
`%LOCALAPPDATA%\Strux` (the dev checkout uses `./data` and `./config.yaml`).

## Data

```
data/events-YYYY-MM.db          one SQLite file per month (events, frames, suggestions, summaries, FTS index)
data/frames/YYYY-MM-DD/*.webp   hot/warm screenshot files (shrunk / deleted by retention)
data/watcher.log                rotating log
```

Delete a month = delete one file. "Forget last 10 min" hard-deletes rows and files.
