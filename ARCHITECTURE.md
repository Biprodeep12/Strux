# Strux (formerly Watcher AI) - architecture and work log

Updated 2026-10-21. Version 0.5.0. Target machine: Windows 11, RTX 3050 6GB
laptop GPU, 16 GB RAM, Ryzen 5 6600H.

## 1. Goal

A desktop assistant that watches everything you do on the PC and offers
timely, short suggestions, with three hard constraints:

1. Fully controlled: every byte stays on disk locally, nothing is sent anywhere.
2. Invisible cost: it must never slow down whatever you are actually doing.
3. Scalable: still fast after a year of data, and easy to extend with new
   sensors, rules and models.

## 2. Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│ UI        React + Vite + Tailwind SPA in WebView2 windows (pywebview):  │
│           dashboard window + frameless always-on-top OVERLAY card       │
│           tray (pystray) · toasts · global hotkeys · CLI                │
├──────────────────────────────────────────────────────────────────────┤
│ API       Starlette/uvicorn on 127.0.0.1:8765 (REST + WebSocket)        │
├──────────────────────────────────────────────────────────────────────┤
│ Brain     rules (free, every 30 s) → local LLM (every 5 min)            │
│           Ask (chat over the log) · vision explain · hour/day summaries │
│           GPU / CPU / idle guards · cooldowns · never-suggest apps      │
├──────────────────────────────────────────────────────────────────────┤
│ Store     SQLite, one file per month, WAL, single writer thread         │
│           events · frames · suggestions · summaries · FTS5 search       │
├──────────────────────────────────────────────────────────────────────┤
│ Sensors   window (WinEvent hook) · browser URL (UI Automation) · idle   │
│           clipboard · screenshot · process (psutil) — threads → Bus     │
│ Bridges   Claude Code hooks → /api/ingest/claude (claude_bridge.py)     │
└──────────────────────────────────────────────────────────────────────┘
```

Why not Tauri/Rust: neither Rust nor the MSVC build tools are installed on
this machine. The UI is a plain SPA talking to a local HTTP/WS API, so it can
be dropped into a Tauri shell later without changes; pywebview (Edge
WebView2) gives the same native window today with zero extra toolchain.

### Module map

| Path | Responsibility |
|---|---|
| `watcher/app.py` | wires bus, store, sensors, brain, retention, API, tray, hotkeys, window |
| `watcher/cli.py` | `run [--no-window] / open / timeline / stats / suggestions / feedback / retention / llm-test` |
| `watcher/config.py` | defaults + `config.yaml` deep-merge |
| `watcher/events.py` | `Event`, `Suggestion`, synchronous `Bus` |
| `watcher/store.py` | monthly SQLite partitions, batching writer, FTS5 index, reads, forget, stats |
| `watcher/priority.py` | BELOW_NORMAL priority + EcoQoS power throttling |
| `watcher/retention.py` | hot → warm → cold → dropped tiering, disk cap |
| `watcher/hotkey.py` | RegisterHotKey thread: Ctrl+Alt+F/P/W |
| `watcher/ocr.py` | RapidOCR (CPU, 2 threads) on an IDLE-priority worker, gated by change + min gap |
| `watcher/api/server.py` | Starlette routes, WebSocket hub, static SPA, settings hot-apply, overlay endpoints |
| `watcher/overlay.py` | bar controller: modes bar / peek / panel, auto-collapse, processing state, content-fit height |
| `watcher/claude_bridge.py` | Claude Code hook payloads → compact `claude` events, session state, approval/finish alerts |
| `watcher/sensors/process.py` | background processes: start / exit / hot CPU / `job_done`, top-N snapshot |
| `hooks/claude_hook.py`, `install-claude-hooks.ps1` | stdlib hook forwarder (always exits 0 in ≤ 1 s) and its settings.json installer |
| `watcher/sensors/winapi.py` | all ctypes Win32 calls in one place |
| `watcher/sensors/window.py` | `SetWinEventHook` foreground + name-change, zero polling |
| `watcher/sensors/browser.py` | address bar of Chrome/Brave/Edge via UI Automation, debounced |
| `watcher/sensors/idle.py` | `GetLastInputInfo` every 5 s |
| `watcher/sensors/clipboard.py` | `GetClipboardSequenceNumber` every 2 s, reads text only on change |
| `watcher/sensors/screenshot.py` | capture → pHash dedupe → video damping → blocklist → OCR → score → WebP |
| `watcher/sensors/phash.py` | 64-bit DCT perceptual hash, pure Python (no numpy, ~6 ms) |
| `watcher/brain/summarizer.py` | bounded activity window, sessions, per-app totals |
| `watcher/brain/rules.py` | 5 deterministic rules, pluggable |
| `watcher/brain/llm.py` | Ollama / OpenAI-compatible client (stdlib HTTP; suggest, chat, images), GPU + CPU guards |
| `watcher/brain/chat.py` | "Ask": period from the question, work log + targeted search hits, strict answer rules |
| `watcher/brain/worklog.py` | raw log → work log: cleaned titles (Claude Code sessions, documents, pages with their site, chats), Claude prompts + files changed per session, finished jobs; noise filtered |
| `watcher/brain/vision.py` | "Explain this screen" with a small VLM, on demand |
| `watcher/brain/scheduler.py` | the brain loop, cooldowns, hour/day summaries, delivery |
| `watcher/ui/window.py` | WindowManager: dashboard + overlay WebView2 windows, created on demand, destroyed on close, DPI-correct placement |
| `watcher/ui/tray.py`, `watcher/ui/toast.py` | tray menu (detached), Windows toasts that open the dashboard |
| `ui/` | React 18 + TypeScript + Vite 6 + Tailwind 4 + Recharts + lucide; built to `ui/dist`; `#/overlay` route = the card |
| `tests/` | 23 unit tests (store, pHash, scoring, rules, brain tick, retention, API, search, timeline) |

## 3. Performance design ("don't affect my other tasks")

| Concern | Decision |
|---|---|
| CPU at idle | window sensor is event-driven (WinEvent hook). Idle and clipboard are one integer syscall per poll. Browser URL is read only on window/title change, debounced to ≤ 1/s. Measured ≈ 0% CPU between captures. |
| Process scheduling | `BELOW_NORMAL` priority class + Windows **EcoQoS** (`SetProcessInformation(ProcessPowerThrottling)`). Verified "EcoQoS: enabled". |
| Disk writes | sensors never touch SQLite. Bounded queue (2000) → one writer thread, batches of ≤ 100 rows, WAL + `synchronous=NORMAL`. |
| Backpressure | queue full ⇒ frames and title-changes are dropped first (`Store.dropped`); window switches and idle events block briefly instead. |
| GPU | model loaded only when the brain decides to ask. Guards: GPU util > 30 %, free VRAM < 4.8 GB, CPU > 60 % ⇒ skip. Ollama `keep_alive: 0` unloads right after the answer. Vision model shares the same rule, one model resident at a time. |
| LLM frequency | at most once per `interval_minutes` (5), only if something happened, only if not idle, never inside the global cooldown. |
| Screenshot cost | downscaled to 1280 px, hashed, discarded if within 6 bits of the previous frame; a playing video (same app+title, pixels changing) is damped to one frame per 90 s. WebP q50 ≈ 60 KB. |
| OCR | off by default. When on: CPU only, single thread, one frame at a time. |
| UI | the API is only hit when the dashboard is open; the WebSocket pushes events, so no polling storms. The window hides instead of closing. |
| Memory | dropped `requests`, `numpy` and FastAPI (-38 MB of imports). WebView2 windows (dashboard, overlay) are destroyed when closed, so their msedgewebview2 children (~150-300 MB) only exist while a window is on screen. CPU 0.5 % idle. |
| OCR | worker thread at THREAD_PRIORITY_IDLE, 2 ONNX threads, at most one frame in flight, only when the screen changed (pHash >= 16 bits) and >= 20 s since the last OCR; ~8-10 s per frame on this CPU. |
| Model warmth | suggestions: `keep_alive: 0` (VRAM freed after each answer, costs ~15 s load each time, latency irrelevant in the background). Interactive chat / explain: `chat_keep_alive: 3m` so follow-ups answer in ~3 s. |

## 4. Screenshot policy: what is kept, what is dropped

Pipeline per trigger (window change or 15 s timer, min 3 s gap):

```
capture → pHash → dedupe (< 6 bits ⇒ drop) → video damping → blocklist → [OCR] → score → save WebP → frames row + FTS
```

Blocklist (Settings → Privacy): password managers, video calls, titles
containing incognito / bank / password / otp / payment. Blocked windows
produce a `blocked` event and **no pixels are read**.

Score (0..100):

| Signal | Points |
|---|---|
| app changed since last frame | +30 |
| big visual change (pHash ≥ 16) or OCR text diff ≥ 40 % | +25 |
| error / stack-trace keywords in title or OCR | +20 |
| clipboard changed within 5 s | +10 |
| user active in last 60 s | +10 |
| idle ≥ 2 min | −30 |
| a suggestion in the following 15 min was **accepted** | +40 and pinned (retroactive) |

Tiers (`retention.py`, runs 1 min after start, then hourly):

| Tier | Keeps | Transition |
|---|---|---|
| hot | OCR text + WebP q50 | after 24 h: score ≥ 30 → warm; 15–29 → cold; < 15 → dropped |
| warm | OCR text + 256 px thumbnail | after 7 days → cold |
| cold | OCR text only, no pixels | after 90 days → row deleted |
| dropped | bare row (ts, app, score) | after 90 days → row deleted |

Pinned frames never fall below warm and are never evicted. Disk cap (2 GB
default on `data/frames`): evict unpinned hot/warm files lowest-score,
oldest-first until under cap. Manual: "Forget last 10 min" (tray, sidebar,
Ctrl+Alt+F) hard-deletes rows, FTS entries and files; delete a month by
deleting its `.db` file.

## 5. Scalability

- **Monthly partitions**: `events-YYYY-MM.db`. All reads are time-windowed and
  touch at most two files. Old months can be archived as a unit.
- **Rolling summaries**: hourly one-liners and a daily summary are written by
  the brain. The LLM prompt is: last 3 hour-summaries + last 15 min raw + last
  5 suggestions. "Ask" adds 7 day summaries + FTS hits for the question.
  Prompt size is constant regardless of DB age.
- **FTS5** index per month over titles, URLs, clipboard previews and OCR text;
  `bm25` ranking with snippets. Backfilled by the v2 migration.
- **Pluggable sensors**: subclass `Sensor`, implement `run()`, emit `Event`s.
- **Pluggable rules**: subclass `Rule`, add to `ALL_RULES`, configure in Settings.
- **Model swap**: `brain.llm.provider: ollama | openai`. Any OpenAI-compatible
  server (llama.cpp, LM Studio, vLLM) by changing one URL in Settings.
- **Schema versioning**: `PRAGMA user_version`; `Store._migrate` is the single
  place to add migrations (v1 base, v2 FTS).

## 6. Model choice (6 GB VRAM)

| Role | Model | Note |
|---|---|---|
| suggestions + Ask | `qwen2.5:7b-instruct-q4_K_M` | ~4.7 GB, 4k ctx, best reasoning at this size |
| faster alternative | `qwen2.5:3b-instruct` / `phi3.5` | ~2.5 GB, < 3 s answers |
| explain screen | `qwen2.5vl:3b` | ~3 GB, on demand only |
| embeddings (future) | `nomic-embed-text` | CPU is fine |

## 6b. The bar (native) and the drop-down pane

**Why native.** The bar used to be a WebView2 page. Every state change resized a
GPU-composited window and Chromium repaints a frame or two late, so hover and
collapse stuttered. Shaping a fixed WebView2 window does not work either: DWM treats
it as opaque and shows the desktop wallpaper behind the clipped area. Colour keys and
WebView2 transparency over WinForms failed the same red-window test.

**`watcher/ui/nativebar.py`.** A Win32 layered window (`WS_EX_LAYERED | TOPMOST |
TOOLWINDOW | NOACTIVATE`) updated with `UpdateLayeredWindow` (per-pixel alpha):
- fixed transparent canvas (560 + padding × 270 logical); the bar is drawn into it, the
  window never resizes; alpha-0 pixels are click-through (verified with WindowFromPoint),
- Pillow renderer: content rendered once per state at 2× and cached; the shape mask is
  built from cached anti-aliased corner sprites; only the bar area is premultiplied;
  ~6 ms per frame,
- width/height follow critically damped springs, content cross-fades (140 ms),
- frames paced by `MsgWaitForMultipleObjects` on the bar's own thread at
  THREAD_PRIORITY_HIGHEST (~60 fps while moving, zero CPU when still),
- states: idle (dot · headline · count), hover (logo, headline, chat / activity /
  dashboard / pause / think), peek (suggestion with accept/snooze/dismiss, or alert),
  open (pane showing, active icon highlighted),
- drag anywhere along the top edge of any monitor (position saved per monitor in
  `data/bar.json`), double-click re-centres, right-click opens the dashboard,
  follows `WM_DPICHANGED` / `WM_DISPLAYCHANGE`,
- Segoe UI Variable (real weights via font variations) + Phosphor icon fonts
  (`assets/fonts`).

**The pane** (`ui/src/pages/Pane.tsx`, route `#/pane`). One WebView2 window created
hidden at startup, so it opens instantly (~65 ms). Shown under the bar with Win32
geometry + WinForms visibility (pywebview's own show/move/resize wait for a `shown`
event that never fires for hidden windows). Tabs and the chat input are pinned; only
the list scrolls; the window is sized to the natural content height, capped at
min(560, 70 % of the screen). Esc or clicking elsewhere closes it (blur is ignored for
600 ms after the bar is pressed or the pane opens); a watchdog resets the bar if the
pane window gets hidden behind its back.

## 6c. Background awareness

- `ProcessSensor`: one `psutil.process_iter` pass every 8 s (~5 ms). Emits
  `proc_start` (after 5 s of life), `proc_exit` (runtime, peak CPU), `proc_hot`
  (≥ 25 % CPU for 3 polls), `job_done` (build/test/install names, ≥ 20 s,
  peak ≥ 5 %), and a `proc_top` snapshot every 30 s (live only, not stored).
  System noise (svchost, conhost, WebView2, shells, Ollama) is ignored.
- `ClaudeBridge`: Claude Code hooks (SessionStart, UserPromptSubmit, Pre/PostToolUse,
  Notification, Stop, SubagentStop, SessionEnd) run `hooks/claude_hook.py`
  asynchronously; it forwards the JSON to `/api/ingest/claude` with a 1 s
  timeout and always exits 0. The bridge keeps per-session state (project,
  status, waiting, counts, last line) and raises bar alerts for permission
  prompts and finishes. Only one-line summaries are stored.
- Both feed the activity summary the rules and the model see
  ("Background jobs finished", "Busy background processes", "Claude Code: …").

## 7. UI design notes

- Palette: black / white / blue only, light theme only (surface `#f6f6f8`, panels white, blue accent
  `#2a78d6`); the `.dark` tokens remain in CSS but are never applied; previously via a `.dark`
  class on `<html>`, remembered in localStorage and shared by the bar window.
  Tokens are CSS variables mapped through Tailwind 4 `@theme inline`, so every
  utility class flips with the theme; both categorical palettes are the
  a single blue ramp (alternating dark/light steps, assigned per app by first appearance, grey past 7) so charts stay inside the palette.
- Framer Motion: sidebar active pill (`layoutId`), page transitions, bar
  layout spring, item enter/exit. Keyboard: `/`, `g d|t|s|a|c`.
- Inter + JetBrains Mono, 1 px hairlines, 16 px radii, violet accent shared with the tray icon.
- Charts follow the data-viz method: one series per chart, categorical colours
  assigned per app in fixed order from the validated dark palette (blue,
  orange, aqua, yellow, magenta, violet, red; 8th+ folds into grey "Other"),
  direct labels on bars, crosshair tooltip on the hourly area chart, text in
  text tokens never series colours.
- Live: one WebSocket per window; events stream into the feed, suggestions
  raise an in-app toast and a Windows toast that opens the dashboard.

## 8. Work log

### 2026-10-20 (v0.1.0, prototype)
1. Checked hardware. No Rust/Ollama installed, so pure Python 3.12 (Astral-managed) in a venv.
2. Config, bus, store (monthly SQLite + writer thread), priority/EcoQoS.
3. Sensors: WinEvent window, idle, clipboard, screenshot (pure-numpy pHash, scoring, blocklist). Optional OCR.
4. Brain: summarizer, 5 rules, LLM client with guards, scheduler, retention tiers + disk cap.
5. Tray + toasts + CLI. 16 unit tests. 25 s live smoke run.

### 2026-10-23 (real-voice "Hey Strux", Strux mark, own-VRAM guard, GPU speech in the exe)
- Live: 35 real bursts, 0 wakes. tiny.en now gets `initial_prompt="Hey Strux."` (SAPI: 30/30 spelled "Strux", 0/24 look-alike
  sentences and no noise woke) + `wake.is_name` fuzzy match ("Starks", "Stucks"; not "stocks", "strong"). Opt-in
  `stt.wake.debug_minutes` logs what the wake check heard, then switches itself off; confirmed on the user's voice
  ("Hey Strux." -> WAKE -> "what's up" answered), debug lines purged after.
- Background LLM skipped "vram free 1.5 GB" while a chat kept qwen warm: `LLM.resident_vram_mb()` (Ollama /api/ps, our
  models only, cached 5 s) now counts as Strux's own VRAM, like resident whisper.
- New mark (watcher/ui/brand.py + Brand.tsx, assets regenerated): geometric white S of two half-round turns whose top
  stroke ends in the old orbiting dot, same blue squircle.
- Packaged exe bundles cublas64_12 + cublasLt64_12 (the only CUDA DLLs a large-v3-turbo transcription loads; cuDNN /
  NVRTC unused): dist/Strux 1.1 GB, GPU speech verified from the bundle alone.

### 2026-10-23 (renamed Halo -> Strux; wake phrase "Hey Strux")
- Every user-facing name, prompt ("You are Strux"), window title ("Strux bar" / "Strux pane"), asset (assets/strux.ico),
  logon task (`Strux`; install-autostart.ps1 removes the old `Halo` task + shortcuts) and packaged exe name (Strux.exe).
  Python package stays `watcher`. Self-filters still also match `halo` / halo.exe so past timeline events stay "not work".
- Wake word: whisper tiny.en writes "Strux" as "Strucks" / "strokes" / "structs" and "Hi" as "high" (SAPI David + Zira at
  3 speeds, 30 clips), so `wake.NAME` covers those spellings (must start with "str": "okay, tracks…" doesn't wake it);
  "Hey Halo" no longer wakes. tests/evals/wake_harness.py: 9/9 scenarios.

### 2026-10-23 ("Hey Strux" fixes; Start Ollama)
Trigger: "Hey Strux" misbehaving. Log + harness (SAPI clips through a fake mic, real VAD + whisper) showed:
- the first wake after a restart waited 30-46 s for large-v3-turbo to load; people repeat "Hey Strux?" meanwhile, and that
  backlog replayed afterwards as 3 stale commands in 20 s (the pane dropped them: busy) plus bogus "nothing after it" alerts.
  Fix: the chat model preloads when the listener starts; audio caught while transcribing is dropped.
- "Hey Strux. Hey Strux, what's up" was sent as "Hey, Strux, what's up". Fix: `wake.message_after` drops every leading wake/greeting.
- with wake on, resident whisper (~1.3 GB) pushed free VRAM under the 4.8 GB guard, so LLM suggestions skipped as
  "vram free". Fix: `GpuGuard.own_mb` (= `stt.vram_mb()`) counts Strux's own model as free.
- pane: a voice message that arrives mid-answer is queued (max 2), not dropped.
- **Start Ollama** (`llm.start_ollama`, POST /api/ollama/start; tray item, Settings -> Model, Ask banner, button on the
  pane's "not reachable" error that re-asks once up): kills a hung ollama, launches "ollama app.exe" outside Strux's job
  (survives Strux restarts), polls until it answers. Local base_url only. Tested live: down -> up 8 s; suspended -> restarted 7.6 s.

### 2026-10-22 (v0.18.0, tool calling you can rely on)
Trigger: by voice, "add a todo …" kept failing while "schedule a meet" sometimes worked. Causes: the wake cleanup cut the
first two words when the small model wrote "Hello" (so "Hello, add a to-do…" became "a to-do…", which missed routing),
and the 7B model's own mistakes. Now measured with a live eval (real model, throwaway planner per case, exact end state
checked): set A 35 cases + set B 14 different ones (follow-ups, renames, shifts, spans, spoken phrasing).
Start: 26/35 on A. After: A 35/35, B 13/14 -> fixed (see below). What changed, all deterministic, in `brain/agent.py`
`_normalise_call`, `brain/dates.py`, `brain/chat.py wants_planner`:
- **Dates are Strux's job**: every date phrase in the message gets its exact date written next to it before the model
  sees it ("friday [Fri 23 Oct 2026]"); a tool argument on another day than the one said is moved to it (the model
  counted weekdays from 2023); `parse_when` reads the copied hint formats; "in 2 hours" is exact.
- **The when the model dropped is read from your words** ("remind me tomorrow morning to …" -> 09:00, "on the 25th"),
  and stripped out of the title; a due / priority you never gave is dropped ("add a todo to call the bank" has none).
- **Only what you asked**: delete needs delete words, complete needs done words in your message ("make a to-do list
  for tomorrow" once deleted same-named TODOs); refusals are logged (`tool … refused: no-intent …`).
- **Mix-ups fixed before running**: event tool used on a TODO and back, add_todo of an existing item with change words
  -> update, remember() for "remind me to" -> TODO, "rename X to Y", "push X by a day" (from X's time), "from 10am to
  6pm" (exact span), "add a note to X: …".
- **Routing**: "to do" as two words, "meet", "add it", done / move statements about an item you have ("I called the
  bank"), follow-ups right after a change ("make it 5pm"); a text-only answer to an add request is retried once.
- "Hey Strux": only greeting words are stripped ("Hello, add …" -> "add …").

### 2026-10-22 (v0.17.0, "Hey Strux"; read-aloud is your choice)
- **"Hey Strux"** (`watcher/wake.py`, Settings -> Voice, off by default because the mic stays open): Silero VAD on the mic;
  each burst of speech's first 3 s go through whisper tiny.en; if it opens with "hey/hi/ok Strux" (also "hey hello"),
  a ding, then everything until 1.2 s of silence (or the rest of the same sentence) is transcribed once with whisper
  small and sent in the bar chat (`overlay.voice_message`, kept 60 s for a pane page that was not running). Stands down
  while Strux is paused, the PC is locked, the mic button records, or a reply is read out (`POST /api/voice/speaking`).
  Nothing said is logged or saved. Sidebar shows "Voice: Hey Strux on" while the mic is open. Simulated-mic test with
  Windows TTS clips: one-breath and pause-then-message both sent correctly, a "hello" sentence ignored; message sent
  ~4 s after you stop talking.
- **Read replies aloud**: Off (default) / When I speak / Every reply, chosen in either chat header or Settings, saved
  in config (`stt.read_aloud`, `GET|PUT /api/voice`). Each reply plays itself once at most.
- Open-loop nudges ("you left X…") are dropped when X was active in the last 15 min.
- **Incident**: a new API test saved its config through `persist_cfg` into the real config.yaml (temp data_dir, sensors
  off, wake on); Strux restarted with it for ~5 min (72 speech bursts checked, none woke, nothing saved). Restored from
  the build's copy. Now `tests/__init__.py` points `config.USER_DIR` at a throwaway folder for every test, and
  `persist_cfg` refuses to write a temp-folder config over a real one.

### 2026-10-22 (v0.16.0, voice in the chats; curated alerts)
- **Speech to text** (`watcher/stt.py`): a mic button in Ask and the bar chat. Strux records the default microphone itself
  (sounddevice; on only between the two clicks, max 120 s) and transcribes on the CPU with faster-whisper `small` int8
  (downloaded once to data/models, ~464 MB; loaded on first use, unloaded after 5 idle min). Your name, "Strux", "Claude
  Code" and TODO projects go in as spelling hints. Enter stops, Esc cancels. 6.7 s of speech -> 2.7 s, word-perfect on a
  test sentence. `POST /api/stt/start|stop|cancel`, `GET /api/stt/status`; config `stt:`.
- **Text to speech**: "Read" on every reply (browser speechSynthesis, local Windows voices only, markdown stripped);
  replies to a dictated question are read out automatically.
- **Curated alerts**: "finished in the background" only for jobs you started (anything under claude.exe is Claude Code's
  tool call and covered by its own alert), not daemons, and only 3+ min (`job_done.min_minutes`). Heads-ups with nothing
  to do leave Activity by themselves after 15 min (`brain.info_expire_minutes`, feedback "expired", never counts as a
  dismissal) and show just OK / X instead of accept / snooze / dismiss.

### 2026-10-22 (v0.15.0, TODO details: notes, files, edit time / priority)
- Clicking a TODO opens its details: title, due (date-time picker, clear, Today 18:00 / Tomorrow 9:00 / +1 day / +1 week),
  priority (Low / Normal / High), project, notes, files. Saves with one PATCH; rows show the first notes line and a
  paperclip count.
- Files under a TODO (`todo_files` table in planner.db, bytes in `data/todo_files/<todo id>/<file id>-<name>`; names
  sanitised, <= 25 MB each, <= 30 per TODO). Upload as raw body (`POST /api/todos/{id}/files?name=`), by button, drop or
  paste; `GET /api/todo-files/{fid}` (inline / `?download=1`), `POST …/open` opens it in its default app, except programs
  and scripts (.exe, .bat, .ps1, .lnk, …) which are only shown in Explorer. Deleting a TODO deletes its files.
- The models see a short notes line and "N files attached" per TODO; the chat can add a note ("add a note to the OS lab
  todo: use the new template", `update_todo.note`, appended, must be the user's own words).

### 2026-10-22 (v0.14.0, better suggestions; screenshot mask; new icon)
- **Suggestions** (every recent model idea had been dismissed: "Add a TODO: review changes in memory.py" again and again):
  - `topic_words` compares suggestions without punctuation / filler, so reworded repeats are caught; anything dismissed in
    the last 3 days is never suggested again.
  - TODO ideas are dropped when they restate the open window, match an existing TODO, or after 3 dismissed TODO ideas;
    a "TODO:" about an existing TODO's deadline becomes a plain nudge.
  - Model ideas must be grounded: at least half their topic words must appear in the log (stops copied prompt examples).
  - The model's interval learns from dismissals like the rules do (2 in a row: 4x, 3: 8x).
  - New prompt: deadline at risk > open loop from earlier > pattern, else NONE; it gets what was accepted / turned down.
    Planner context now gives "(in 2 h 15 min)" so the model never does time arithmetic.
  - Noise: background daemons (Gradle, tsserver, …) and jobs in background-agent worktrees are not "finished jobs";
    `except ValueError:`-style code and Strux's own quoted text are not "errors on screen"; long-overdue TODOs remind once
    a day ("overdue since …, done or move it?"); test suggestions ([e2e], rule:test) never teach the cooldowns.
- **Timeline screenshots**: Strux masked its whole bar *window* (544x270 px, mostly transparent) in white. Now only the
  drawn part (`NativeBar.visible_rect`) and the open pane are painted over, with the colour around them.
- **Icon**: new mark (ring + orbiting dot, blue gradient; `watcher/ui/brand.py`, `assets/make_icon.py`) for the exe, tray,
  bar, favicon and sidebar. Windows get `assets/strux.ico` and their own taskbar id (`Strux.Assistant`), so they no longer
  show the Python icon.

### 2026-10-21 (v0.13.0, Memory; clear timeline)
- **Memory** (`watcher/memory.py`, `data/memory.db`): editable blocks (About me, Preferences, Current focus, Notes, plus your
  own). Switched-on blocks go into every model prompt (chat, files / images, TODO tools, plan, suggestions; <= 3,000 chars)
  via `prompts.with_memory`. Memory page (`#/memory`): edit / rename / switch off / delete blocks (saves on blur), plus
  "What the AI sees" (`GET /api/memory/preview?q=`: the exact system prompt + context, no model call). Dashboard card
  with "Remember that…". Chat: "remember that …" / "forget that …" (tools `remember`, `forget_memory`); only facts the
  user typed can be saved, never from a file / image or invented by the model.
- **Clear timeline** (Settings, type DELETE; `POST /api/control/clear-timeline {"confirm":"DELETE"}`): every monthly
  database's events, screenshots, summaries and search index, all image files, then VACUUM + WAL checkpoint so the text
  is not even left in free pages (verified by a test that greps the raw database file). TODOs, calendar, Memory,
  suggestions and settings stay. First run: 19,047 events, 1,180 screenshots, 1,181 files; 6.4 MB -> 84 KB.
- Every TODO / event delete and completion is now written to watcher.log with its caller (`planner._audit`).

### 2026-10-21 (v0.12.0, AI hardening from an edge-case sweep; Claude alerts only when they need you)
- **The "weird" day-wise TODO reply**: intent checks ran on the inlined image text (words like "set" / "due" in the
  timetable turned on "update existing"), dedupe was by title only (a plan reuses "Java preparation" per day, so
  each day overwrote the first), the model spread "consolidation" over 16 days and repeated whole batches, and dues
  came as "YYYY-MM-DD 00:00". Now: intent = what you typed; same title on another day = another TODO; identical
  calls ignored, <= 4 days per title, <= 40 calls per message, a round that changes nothing ends the loop;
  date-only dues mean 21:00 that day; "a; b" plan lines become two TODOs; the reply is one line per TODO in its
  final state, grouped by day for long plans.
- **Safety** (found by tests, one by a live run that deleted a real TODO, restored):
  - delete / complete / update / move only on items the user *named* (title word or #id) — "delete all my todos"
    changes nothing and says how to clear many; still <= 3 deletes per message
  - with attached files / images the model gets add + list tools only, so a file saying "delete every TODO" can't
  - an empty / one-letter / '%' name used to match every TODO (LIKE '%%'): now matches nothing
  - TODO / event titles must come from the user's words (typed or attached); "add a todo" asks what it should say
  - the model claiming "Added / Scheduled …" without a tool call is never passed on (both tool and plain chat paths)
- **Input**: "25:00" / "10:75" and "from 5pm to 3pm" are asked back; "schedule|book|remind|add … " at the start of a
  message routes to the tools; "mark #12 done" works; "#108 renew the domain" resolves the id; model dates in a past
  year ("2023-10-21") are moved to this year; numbers / "nan" / 1e20 are not times.
- **Robustness**: prompts are fitted to the context window (Ollama truncated 8006 -> 2050 tokens with 3 chats at
  once and returned 500); /api/chat validates messages and never returns a bare 500; non-object JSON bodies and bad
  field types give 400 on every planner route (fuzz: 897 junk requests, 0 server errors).
- **Claude Code alerts** (`claude_bridge.py`): approval = "Claude needs your OK in watcher-ai: run “npm run build”";
  finished = "Claude finished in watcher-ai after 4 min: “fix the dashboard spacing”", only if the turn you started
  stayed finished for 8 s (a session woken again by a background task is not finished), took >= 15 s, and you are not
  already in a terminal / editor. No alert for Claude Code's idle reminder, subagents, or sessions nobody typed into.
- **Tests**: 93 unit / API (new: tests/test_ai_edge_cases.py, tests/test_claude_alerts.py); live adversarial 17/17
  with a before/after check that the user's TODOs and events are untouched; all earlier live suites green.

### 2026-10-21 (v0.11.2, images -> TODOs, the black-screen / HTTP 500 crash, Strux reading its own bar)
- **Incident 19:22:52**: "retry and add it to the todo list" with a screenshot in the chat sent the image + history to
  the 3B vision model; after ~2.5 min of generation Ollama's runner died (`llama-server terminated 0xc0000409`) and
  at the same second Windows logged nvlddmkm 14 / 153 (GPU engine reset): the screen went black for a moment and
  the chat got HTTP 500. The vision model also can't call tools, which is why "make day wise todo of this" only
  printed a list.
- **Fix**: images are *read* once by the vision model into text (prompt "Read an attached image"; <= 896 px,
  num_predict 900, num_batch 128 so GPU kernels stay far under the 2 s TDR watchdog, keep_alive 0, retry once at
  640 px, cached per image), inlined into their message, and the 7B text model answers, with the TODO / calendar
  tools when asked. A crash returns an explanation instead of a 500. Live: the same flow made 7 TODOs across
  22–25 Oct twice with zero display-driver events; the follow-up reused the cached read.
- Agent replies are now truthful when nothing changed ("All of these are already on your list…"), and a re-added
  TODO only changes its time / priority when the message asks for a change ("retry" never moves anything).
- The `job_done` "vite dev server … finished after 3097 min" came 15 min earlier (19:07) when the dev server exited;
  unrelated to the crash. Long-running servers now read "stopped after running for 2 d 3 h. Expected?".
- OCR read Strux's own bar text ("Error in Explorer: “Error in Explorer: …”"): Strux's bar and pane are now painted
  out of every screenshot before hashing / OCR, and Strux's own suggestion phrases are ignored.

### 2026-10-21 (v0.11.1, paste + drop anywhere, drop on the bar, Ctrl+Alt+V)
- Paste and drag & drop work anywhere on the pane / Ask page (document-level listeners), not only in the input;
  every stray file drop is taken, so WebView2 can never navigate to a dropped file. Plain text paste is untouched.
- Files copied in Explorer: a page only receives clipboard *images*, so a paste without text or files asks the backend
  (`POST /api/attach/clipboard`, Pillow `ImageGrab.grabclipboard` -> image or CF_HDROP paths).
- The native bar accepts files (WS_EX_ACCEPTFILES + WM_DROPFILES): they open the bar chat with the files in its tray
  (`overlay.attach`, kept in `attach_log` for a pane that has not loaded yet). Ctrl+Alt+V does the same with the
  clipboard (screenshot, copied image or copied files).

### 2026-10-21 (v0.11.0, files + images in chat, model shown everywhere, layout fixes)
- **Attachments** (`brain/attachments.py`): paperclip, paste or drag & drop in the bar chat and on Ask. Images are
  shrunk to <= 1280 px JPEG (browser and server) and answered by the vision model (Qwen2.5-VL 3B, prompt
  "Chat: images"); documents (text, code, md, json, csv, logs, html, PDF via pypdf, .docx read from its XML) are
  inlined into the message (12k chars per file, 24k per request), answered with the "Chat: files" prompt, or by
  the TODO tools ("add TODOs from these notes"). Unreadable files return a clear error instead of being ignored.
  Live: vision read an error screenshot, PDF total, code summary, notes → TODOs, UI attach / remove / send.
- **Which model**: every AI answer reports its model (`model` in /api/chat, /api/plan, action results); a
  model tag shows under chat replies (pane + Ask), Plan my day, screenshot explanations, on model suggestions,
  on each prompt in Settings, in the dashboard header and in the sidebar status card.
- **Layout**: Settings uses the full width in two independent columns (no gaps under short cards); the sidebar
  footer is a status card (model, GPU, uptime, queue only when non-zero) + the action buttons + a "Keyboard
  shortcuts" popover instead of a wrapped line of key caps.
- **Chat tools, from real use**: due phrases like "today 21 Oct", "Oct 21 5pm", "21/10" now parse; a model that
  "re-adds" an open TODO to change its time now updates it (it used to keep the old time while reporting the new).

### 2026-10-21 (v0.10.0, TODOs + calendar, chat tools, editable prompts, more suggestions)
- **Planner** (`watcher/planner.py`, `data/planner.db`, never rotated): TODOs (title, notes, due, priority, project,
  source you / ai / suggestion) and calendar events. Ids are AUTOINCREMENT (a reused id once made a new TODO inherit
  an old one's "already reminded" state). Quick add: "Fix login tomorrow 10am !high #watcher-ai". Every change is
  broadcast (`{"type":"planner"}`) so open pages refresh live.
- **UI**: TODOs page (grouped Overdue / Today / Tomorrow / This week / Later / No date, done tab, double-click rename),
  Calendar page (month grid, day agenda, event editor), dashboard "Today" row (TODOs + quick add + Plan my day +
  agenda), chat replies show chips for what the model changed.
- **Chat tools** (`brain/agent.py`): add / complete / update / delete TODOs, add / move / delete / list events, via
  Ollama tool calling. Routed only for planner-ish messages (`chat.PLANNER_Q`). Guard rails for the 7B model:
  stated durations ("for 30 minutes") are applied if the model forgets them, "move X" answered with add_event(X)
  becomes a move, the confirmation is built from the actual changes (exact titles / times), and a period with an
  empty log is answered without the model (it invented work for an empty recap).
- **Editable prompts** (`brain/prompts.py`): chat, chat-right-now, chat TODOs & calendar, plan my day, background
  suggestions, explain error, vision, recap, ask. Settings → AI prompts edits / resets them (`GET/PUT /api/prompts`,
  stored under `prompts:` in config.yaml); placeholders that are missing or malformed never break a call.
- **More suggestions** (rules): todo_due (Mark done), event_soon (Prep me), morning_plan (Plan day), wrap_up (after
  21:00), busy_process (something at ~20 % CPU for 30+ min), revisit_page (same page 4+ times today → Add TODO),
  distraction (25+ min of videos / social in 45 min → focus block). The model may answer "TODO: …" → "Add TODO".
  Reminders skip the global cooldown and the dismiss-learning, and are de-duplicated per item (id + creation time).
  Global cooldown 10 → 6 min.
- **Tests**: 46 unit / API tests (planner, parsing, prompts, agent with a scripted model, every rule, actions, API).
  Live end-to-end against the real model and app: chat tools 13/13 (twice), suggestions + actions 8/8,
  headless-Edge UI 27/27 (TODOs, calendar, prompts editor, dashboard, pane, all routes, no page errors).

### 2026-10-21 (v0.9.0, welcome back, personal answers, tracking accuracy, perf)
- **Recap that "did nothing"**: the pane window is created hidden, and its page only starts when first
  shown, so an action result broadcast before that was lost. Results are now also kept server-side
  (`overlay.chat_log`, 10 min) and the pane merges them by id from `/api/overlay/state`. `index.html` is
  served `no-cache` and windows load `/?v=<build>`, so WebView2 can never run a stale bundle after an update
  (a stale shell pointing at deleted chunks is another cause of blank pages).
- **Welcome back** (`brain/welcome.py`): fired on the `idle_end` event itself (≥ 10 min away), instant, no
  model: "Welcome back, Biprodeep · 25 min away. You were working with Claude Code on watcher-ai: “…”. While you
  were away: Claude Code finished in watcher-ai; npm run build finished (4 min)." Name = `user.name` in config,
  else the Windows display name. Accept = Recap (model, 90 min before the break, ends with "Next:").
- **Personal answers**: second person, the user's name, a "Next:" line for unfinished work, hedge words removed.
  "What's running / what is Claude doing" questions are answered from live state (top processes with job
  labels, Claude sessions) instead of the day's log. "Ask" on a suggestion now gets the model's why / how.
- **Tracking accuracy**:
  - locking the PC counts as away at once; a fullscreen window without input (video, slides) is not idle
  - terminal time during Claude Code hook activity counts as "working with Claude Code on <project>"
  - desktop / taskbar / lock screen / Strux windows no longer count as app time (Explorer was 35 min/day of noise)
  - jobs are named from their command line: "vite dev server in Untrue", "npm run dev", "pytest tests"
  - windows titled with an .exe path (updaters, installers) are dropped from the work log
  - a crashing sensor restarts with back-off instead of going dark
- **Performance**: the process sensor reads the whole process table with one `NtQuerySystemInformation` call
  (17 ms vs 3.3 s of psutil per-process opens on a cold pass); Strux went from 4.7 % to 1.8 % of one core
  (process sensor 2.0 → 0.03 CPU-s/min). ui-boost uses the same snapshot. The work log reads events once
  instead of three times (~20 ms for 3 h). `/api/debug/threads` shows CPU per thread.

### 2026-10-21 (v0.8.0, actionable suggestions + audit)
- **Suggestions do things.** Each carries an action (`watcher/brain/actions.py`); the accept button is labelled with it
  (bar, pane, dashboard) and the result lands in the pane chat:
  | rule / source | text | accept runs |
  |---|---|---|
  | error_on_screen | quotes the new error line | `explain_error`: OCR text → text model (vision model if no text) |
  | welcome_back | "Welcome back (2 h away). Recap?" | `recap`: work log of the 90 min before the break |
  | app_switch_storm | "30 switches in 15 min…" | `focus` timer 25 min: only urgent rules (errors, jobs) fire meanwhile |
  | long_focus | "70 min straight in VS Code…" | `break` timer 5 min |
  | clipboard_repeat, model | — | `discuss`: opens the chat with it |
- Snooze brings a suggestion back after `brain.snooze_minutes` (30). Dismiss teaches: a rule whose last 2 / 3
  suggestions were dismissed waits 4x / 8x its cooldown.
- Error detection on OCR text uses real error output patterns (Traceback, `XxxError:`, `npm ERR!`, `error TS1234`…)
  and only lines that were not already on screen; Strux's own windows never count.
- Model suggestions get the work log (projects, pages, Claude prompts) instead of app names; near-duplicates of
  the last 4 h are dropped.
- Screenshot loop: no capture of the lock screen or Strux's own windows; on GDI failures it backs off (up to 5 min)
  with one log line instead of a traceback every 15 s, and recreates its handles.
- Dashboard blank screens: framer-motion page transitions (`AnimatePresence mode="wait"`) replaced with a CSS fade
  that cannot stall at opacity 0; every page sits in an error boundary (errors are logged to watcher.log via
  `/api/client-error`); the page remounts with fresh data after the window was hidden > 60 s.
- Perf: pages are lazy chunks (the pane loads 330 KB instead of 834 KB, no charts); the pane ignores non-Claude
  live events; charts memoized; Timeline thumbnails are plain CSS cards with `content-visibility` and the list keeps
  its identity when nothing changed; `store.stats()` COUNT(*) cached 30 s; dead WebView2 overlay code removed.

### 2026-10-21 (v0.7.1, work-focused answers)
39. Ask / chat answers describe work, not apps: `worklog.py` builds a task-level log (Claude Code sessions and prompts, files changed, pages paired with their site from URL events, chats, jobs), drops noise, phrases pages as actions so the 7B model does not misread titles; prompt limits answers to 5 one-line bullets with no counters or process names.

### 2026-10-21 (v0.7.0, native bar)
37. Bar rewritten natively (layered window, Pillow renderer, springs, drag-to-slide, multi-monitor, DPI); chat/activity moved to the hidden-at-start pane window.
38. Pane layout: pinned tabs + input, single scrolling list, content-sized window with a cap; tab switches resync with window visibility.

### 2026-10-21 (v0.6.1, region shaping reverted)
36. Region shaping reverted (dark rectangle over other apps, see 6b); bar resizes per state again.

### 2026-10-21 (v0.6.0, region-shaped bar)
34. Fixed the squashed Activity panel (it measured its own window height and fed it back).
35. Bar rebuilt on a fixed-size window shaped by an animated window region (`RegionAnimator`); no more window resizes, click-through outside the shape, panel content-sized. Colour-key layering was tried first and rejected: WebView2's composited output is not part of the layered surface, so the whole window disappeared.

### 2026-10-21 (v0.5.3, slim strip)
33. Idle strip width fits its text; the card fills the window exactly (no CSS border/radius/shadow) the DWM border and the window shadow are turned off, so there is one clean rounded edge; OCR stage hidden from the bar.

### 2026-10-21 (v0.5.2, smoothness)
30. `watcher/ui/boost.py`: WebView2 renderer children get NORMAL priority with EcoQoS off (they inherited BELOW_NORMAL + throttling from Strux and dropped frames under load).
31. One pre-measured resize per mode change (page measures after render and sends mode + height together); exiting elements leave layout (`popLayout`) so the measure is exact; the observer self-heals after 350 ms; blur filters removed; mode changes logged.
32. Light-only theme, white strip; dark tokens kept in CSS but never applied.

### 2026-10-21 (v0.5.1, edge strip + palette)
28. Bar rebuilt as the top-edge strip (user reference screenshot): idle strip → hover → peek / chat / panel, one width, bleed above the edge, always dark; single-step geometry changes.
29. Black / white / blue palette across the app, blue logo and tray icon.

### 2026-10-21 (v0.5.0, Strux)
25. Renamed to **Strux** (ring logo mark: tray, .ico, favicon, window titles, task `Strux`, shortcuts, `%LOCALAPPDATA%\Strux`, `dist/Strux/Strux.exe`). Package stays `watcher`.
26. Bar rebuilt as the idle → hover → peek / chat / panel pill with page-driven width + height, startup animation, spring transitions.
27. UI pass: Phosphor duotone icons, `PageHeader`, animated nav, segmented controls, skeletons, empty states, Settings grouped with a Bar section.

### 2026-10-21 (v0.4.0, background awareness + bar + light UI)
19. `ProcessSensor` (starts / exits / hot / job_done / top-N) and `JobDone` rule.
20. Claude Code bridge: hooks installed in `~/.claude/settings.json` (merged with the existing sound hooks), `hooks/claude_hook.py`, `/api/ingest/claude`, session state, approval / finish alerts; verified live from this very session.
21. Overlay rebuilt as the top-centre bar with bar / peek / panel modes, content-fit height, DPI-correct placement, Framer Motion.
22. Light theme by default with dark toggle; dashboard UX pass (greeting, skeletons, Claude + Background cards, keyboard shortcuts, animated nav).
23. Window sensor ignores our own windows; WebSocket status now carries the same fields as `/api/status`.
24. Bar window is a WS_EX_TOOLWINDOW (no taskbar / Alt+Tab entry), fills its window (no dark frame), `POST /api/suggestions/test` for demos. Screenshots in `docs/screens/`.

### 2026-10-21 (v0.3.0, gaps + overlay + no-terminal start)
14. Installed Ollama + `qwen2.5:7b-instruct-q4_K_M` + `qwen2.5vl:3b`; chat and "Explain this screen" verified end to end through the API.
15. Installed RapidOCR; OCR moved to an idle-priority worker with change/min-gap gating; OCR text indexed for search; OCR-detected errors feed the error rule.
16. Memory: removed `requests`, `numpy`, FastAPI; WebView2 released whenever no window is open (pywebview restart verified).
17. Overlay card (suggestions / alerts / processing / quick chat) + `Ctrl+Alt+Space`; DPI-aware placement.
18. No-terminal start: logon task + Start Menu / Desktop shortcuts (`install-autostart.ps1`), single-instance launch (second start shows the window), app icon.

### 2026-10-20 (v0.2.0, full app)
6. Store v2: FTS5 search table + migration/backfill; sessions, per-app totals, day summaries.
7. FastAPI + WebSocket API, settings hot-apply persisted to `config.yaml`.
8. Browser URL sensor (UI Automation), global hotkeys, never-suggest apps, video damping.
9. LLM: generic chat + images; "Ask" context builder; vision "Explain this screen".
10. React/Vite/Tailwind dashboard (6 pages) inside a WebView2 window; tray runs detached.
11. 23 tests passing; every API endpoint smoke-tested; all six pages screenshotted with headless Edge.
12. PyInstaller spec + `launcher.py` entry, `build.ps1`, `install-autostart.ps1` (Task Scheduler), `run.ps1` builds the UI on first run.
13. Frozen builds store data + config in `%LOCALAPPDATA%\WatcherAI` (`config.USER_DIR`); the bundled `config.yaml` is copied there on first run.

## 9. Next steps

- Embeddings (`sqlite-vec` + `nomic-embed-text`) for semantic "what was I doing when…".
- Streaming answers in Ask / overlay; suggestion actions (start a focus timer, open the PR).
- Faster OCR (smaller detector or GPU ONNX runtime) if the 8-10 s/frame CPU cost shows up in practice.
- Tauri 2 shell if a Rust toolchain gets installed (UI drops in unchanged).
- Weekly report page; per-app goals; export a day as Markdown.

## v0.19: screen answers, Knowledge, learner (2026-10-22)

- **"What's on my screen?"** (`watcher/brain/screen.py`): routed before the work log. Captures the monitor of the
  last non-Strux window (`overlay._current_hwnd`), paints over Strux's own windows, OCRs it (CPU) and answers with the
  `screen` prompt from window title + URL + OCR text. Follow-ups within 5 min reuse the same look; the pane's eye
  button forces a new one (`look: true`). Blocklisted apps/titles are never captured.
  **Vision is OFF** (`screen.vision: false`): a full-screen qwen2.5vl read crashed Ollama's CUDA runner and reset the
  display driver on 2026-10-22 18:20 (nvlddmkm 13/14/153). If switched on: 448 px, num_batch 64.
- **Knowledge** (`watcher/knowledge.py`, `data/knowledge.db`): notes from Claude Code transcripts
  (`~/.claude/projects/*/*.jsonl`, incremental per-file offsets, Stop hook + every 5 min, 14-day backfill), Strux chats
  (also the pane's chat history: `messages` table) and screen looks. FTS5 recall goes into work-log answers;
  thumbs up/down on answers become liked / disliked examples for similar questions. Screen notes go with
  Clear timeline / Forget 10 min; the rest stays like Memory (clear per source on the Memory page).
- **Learner** (`watcher/brain/learner.py`): every 3 h (when not busy) rewrites the "Learned" Memory block
  (<= 12 facts; secrets/hedges dropped) from Claude prompts, chats + ratings, screens and the 8 h work log.
  A switched-off Learned block is left alone. `POST /api/knowledge/learn` runs it now.
- Pane: one-line header (icon buttons: read aloud, history, new chat), live chips (`app.chips()`), "In front" line,
  screen thumbnail on screen answers, busy hint ("Looking at your screen…"), last conversation restored within 6 h.
