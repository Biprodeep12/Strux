"""Local HTTP + WebSocket API (127.0.0.1 only) that the dashboard UI talks to.

Built on Starlette directly (FastAPI + pydantic would add ~15 MB of RSS and we
only ever parse small JSON bodies by hand).

REST
  GET  /api/status
  GET  /api/timeline?minutes=120          sessions + markers
  GET  /api/apps?hours=24                 seconds per app (chart)
  GET  /api/frames?minutes=120&limit=200
  GET  /api/frames/{id}/image             WebP (hot) or thumbnail (warm), 404 otherwise
  POST /api/frames/{id}/explain           vision model on demand
  GET  /api/suggestions?limit=50
  POST /api/suggestions/{id}/feedback     {"value": "accept|dismiss|snooze"}
  GET  /api/search?q=...
  GET  /api/summaries?period=day|hour
  GET  /api/models                        Ollama reachability + installed models
  POST /api/ollama/start                  bring a local Ollama up (kills a stuck one first)
  GET  /api/settings   PUT /api/settings  (persisted to config.yaml, hot-applied)
  POST /api/control/pause {"paused": bool} | /forget {"minutes": n} | /think | /show
  POST /api/chat {"messages": [...], "conv"?: id}   (conv = keep it in the chat history)
  GET  /api/chats   GET|DELETE /api/chats/{conv}   POST /api/chats/rate {"id", "rating": 1|-1|0, "why"?}
  GET  /api/knowledge   POST /api/knowledge/learn   POST /api/knowledge/clear {"source"}   GET /api/knowledge/notes
WS
  /ws  -> {"type": "event"|"suggestion"|"status", ...}
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import config as config_mod
from ..brain import chat as chat_mod
from . import planner_routes
from ..brain import summarizer, vision
from ..events import Event

log = logging.getLogger(__name__)
UI_DIST = config_mod.ROOT / "ui" / "dist"


def _row(r) -> dict | None:
    return dict(r) if r is not None else None


def _apply_in_place(dst: dict, src: dict) -> None:
    """Recursively copy src into dst without replacing nested dict objects
    (sensors/brain hold references to those sub-dicts)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _apply_in_place(dst[k], v)
        else:
            dst[k] = v


async def _json(request: Request) -> dict:
    try:
        body = await request.body()
        data = json.loads(body) if body else {}
        return data if isinstance(data, dict) else {}       # a list / number / string body is not a request
    except Exception:
        return {}


def _q(request: Request, name: str, default: float) -> float:
    try:
        return float(request.query_params.get(name, default))
    except ValueError:
        return default


class Hub:
    """Fan-out of bus events to WebSocket clients, thread-safe."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()

    def push(self, msg: dict) -> None:
        if self.loop and self.clients:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._send(msg)))

    async def _send(self, msg: dict) -> None:
        data = json.dumps(msg, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


def create_api(app) -> Starlette:
    hub = Hub()
    store = app.store

    def on_event(ev: Event) -> None:
        hub.push({"type": "event", "kind": ev.kind, "ts": ev.ts, "app": ev.app, "title": ev.title, "data": ev.data})

    app.bus.subscribe("*", on_event)
    app.on_suggestion.append(lambda s: hub.push({"type": "suggestion", "id": s.id, "ts": s.ts, "source": s.source, "text": s.text}))
    app.broadcasters.append(hub.push)

    def full_status() -> dict:
        st = app.status()
        st["llm"] = {"model": app.brain.llm.model, "vision_model": app.cfg["brain"]["llm"].get("vision_model", ""),
                     "base_url": app.brain.llm.base, "last_latency": app.brain.llm.last_latency}
        st["gpu"] = app.brain.gpu.status()
        st["sensors"] = {s.name: not s.paused.is_set() for s in app.sensors}
        ss = next((s for s in app.sensors if s.name == "screenshot"), None)
        if ss:
            st["screenshot"] = {"captured": ss.captured, "deduped": ss.deduped, "blocked": ss.blocked,
                                "ocr_done": ss.ocr_done, "ocr_skipped": ss.ocr_skipped}
        return st

    async def status_ticker():
        while True:
            await asyncio.sleep(5)
            if hub.clients:
                try:
                    await hub._send({"type": "status", **full_status()})
                except Exception:
                    pass

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        hub.loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(status_ticker())
        try:
            yield
        finally:
            task.cancel()

    # ---- read endpoints ---------------------------------------------
    async def status(request: Request):
        return JSONResponse(full_status())

    async def timeline(request: Request):
        now = time.time()
        sess, markers = summarizer.sessions(store, now - _q(request, "minutes", 120) * 60, now)
        return JSONResponse({"sessions": sess, "markers": markers, "now": now})

    async def apps(request: Request):
        now = time.time()
        hours = _q(request, "hours", 24)
        totals = summarizer.app_totals(store, now - hours * 3600, now)
        return JSONResponse({"apps": [{"app": a, "seconds": int(s)} for a, s in totals], "hours": hours})

    async def frames(request: Request):
        now = time.time()
        rows = store.frames_between(now - _q(request, "minutes", 120) * 60, now, int(_q(request, "limit", 200)))
        return JSONResponse({"frames": [_row(r) for r in rows]})

    async def frame_image(request: Request):
        r = store.frame(int(request.path_params["frame_id"]))
        if not r or not r["image_path"] or not Path(r["image_path"]).exists():
            return JSONResponse({"detail": "no image for this frame"}, status_code=404)
        return FileResponse(r["image_path"], media_type="image/webp", headers={"Cache-Control": "private, max-age=3600"})

    async def frame_explain(request: Request):
        body = await _json(request)
        r = store.frame(int(request.path_params["frame_id"]))
        if not r:
            return JSONResponse({"detail": "frame not found"}, status_code=404)
        model = app.cfg["brain"]["llm"].get("vision_model", "qwen2.5vl:3b")
        return JSONResponse(await asyncio.to_thread(vision.explain_frame, app.brain.llm, r, model, body.get("question")))

    async def suggestions(request: Request):
        rows = store.suggestions_page(int(_q(request, "limit", 50)), int(_q(request, "offset", 0)))
        return JSONResponse({"suggestions": [_row(r) for r in rows]})

    async def suggestion_test(request: Request):
        """Push a suggestion through the normal pipeline (store + bar + toast). Handy for demos."""
        from ..events import Suggestion
        body = await _json(request)
        text = str(body.get("text") or "Test suggestion: this is how a suggestion pops out of the bar.")[:200]
        ctx = {"action": body["action"]} if isinstance(body.get("action"), dict) else {}
        s = app.brain._deliver(Suggestion(text=text, source=str(body.get("source") or "rule:test"), context=ctx))
        return JSONResponse({"ok": True, "id": s.id})

    async def feedback(request: Request):
        body = await _json(request)
        value = body.get("value")
        if value not in ("accept", "dismiss", "snooze"):
            return JSONResponse({"detail": "value must be accept|dismiss|snooze"}, status_code=400)
        sid = int(request.path_params["sid"])
        if not app.suggestion_feedback(sid, value):
            return JSONResponse({"detail": "suggestion not found"}, status_code=404)
        return JSONResponse({"ok": True})

    async def debug_welcome(request: Request):
        """Run the welcome-back flow as if you had been away `minutes` (testing without waiting)."""
        body = await _json(request)
        try:
            minutes = max(0.0, min(24 * 60.0, float(body.get("minutes", 25))))
        except (TypeError, ValueError):
            return JSONResponse({"detail": "minutes must be a number"}, status_code=400)
        s = await asyncio.to_thread(app.welcome_back, minutes * 60)
        return JSONResponse({"ok": bool(s), "id": s.id if s else None, "text": s.text if s else None})

    async def debug_threads(request: Request):
        """CPU seconds per thread of this process (profiling without extra tools)."""
        import psutil, threading
        names = {t.native_id: t.name for t in threading.enumerate()}
        rows = [{"name": names.get(t.id, f"native-{t.id}"), "cpu_s": round(t.user_time + t.system_time, 2)}
                for t in psutil.Process().threads()]
        return JSONResponse({"threads": sorted(rows, key=lambda r: -r["cpu_s"]),
                             "rss_mb": round(psutil.Process().memory_info().rss / 2**20)})

    async def debug_attach(request: Request):
        """Same path as files dropped on the bar (testing without dragging)."""
        body = await _json(request)
        if app.overlay:
            paths = body.get("paths")
            if not isinstance(paths, list):
                return JSONResponse({"detail": "paths must be a list"}, status_code=400)
            await asyncio.to_thread(app.overlay.attach_paths, [p for p in paths if isinstance(p, str)])
        return JSONResponse({"ok": True})

    async def attach_clipboard(request: Request):
        """What's on the Windows clipboard as attachments (a pasted page only sees images, not files copied in Explorer)."""
        from ..brain import attachments
        atts, problems = await asyncio.to_thread(attachments.from_clipboard)
        return JSONResponse({"attachments": atts, "problems": problems})

    async def client_error(request: Request):
        body = await _json(request)
        log.warning("UI error on %s: %s", str(body.get("page"))[:40], str(body.get("message"))[:2000])
        return JSONResponse({"ok": True})

    async def search(request: Request):
        return JSONResponse({"hits": store.search(request.query_params.get("q", ""), int(_q(request, "limit", 30)))})

    async def summaries(request: Request):
        rows = store.summaries(request.query_params.get("period", "day"), int(_q(request, "limit", 30)))
        return JSONResponse({"summaries": [_row(r) for r in rows]})

    async def models(request: Request):
        llm = app.brain.llm
        return JSONResponse(await asyncio.to_thread(lambda: {"reachable": llm.reachable(), "models": llm.models()}))

    async def ollama_start(request: Request):
        from ..brain.llm import start_ollama
        out = await asyncio.to_thread(start_ollama, app.brain.llm)
        app.broadcast({"type": "ollama", **out})
        return JSONResponse(out)

    # ---- settings --------------------------------------------------------
    async def get_settings(request: Request):
        return JSONResponse(app.cfg)

    def persist_cfg() -> None:
        """Write the live config to config.yaml (data_dir only if it differs from the default)."""
        cfg_path = config_mod.config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        to_write = dict(app.cfg)
        if to_write.get("data_dir") == config_mod.DEFAULTS["data_dir"]:
            to_write.pop("data_dir", None)
        # a config whose data lives in a temp folder is a test's: it must never replace the real config.yaml
        import tempfile
        temp = Path(tempfile.gettempdir()).resolve()
        if to_write.get("data_dir") and temp in Path(to_write["data_dir"]).resolve().parents \
                and temp not in cfg_path.resolve().parents:
            log.error("refusing to write a temp-folder config over %s", cfg_path)
            return
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(to_write, f, sort_keys=False, allow_unicode=True)

    async def put_settings(request: Request):
        body: dict[str, Any] = await _json(request)
        body.pop("data_dir", None)  # cannot be changed at runtime
        body.pop("prompts", None)   # managed by PUT /api/prompts, so a stale settings form can't overwrite prompt edits
        body.pop("stt", None)       # voice: managed by PUT /api/voice (it also starts / stops the "Hey Strux" listener)
        _apply_in_place(app.cfg, body)
        persist_cfg()
        app.brain.llm.__init__(app.cfg["brain"]["llm"])
        log.info("settings updated via API")
        return JSONResponse({"ok": True})

    # ---- speech to text (the mic button in the chats) -------------------------------------------
    def _stt_off():
        return None if app.cfg.get("stt", {}).get("enabled", True) else JSONResponse({"detail": "speech to text is off in Settings"}, status_code=403)

    async def stt_start(request: Request):
        if (off := _stt_off()):
            return off
        try:
            return JSONResponse(await asyncio.to_thread(app.stt.start))
        except Exception as e:
            log.warning("microphone failed: %s", e)
            return JSONResponse({"detail": f"Couldn't open the microphone: {e}"}, status_code=409)

    async def stt_stop(request: Request):
        if not app.stt.recording:
            return JSONResponse({"detail": "not recording"}, status_code=409)
        try:
            return JSONResponse(await asyncio.to_thread(app.stt.stop))
        except Exception as e:
            log.exception("transcription failed")
            return JSONResponse({"detail": f"Couldn't transcribe: {e}"}, status_code=500)

    async def stt_cancel(request: Request):
        app.stt.cancel()
        return JSONResponse({"ok": True})

    async def stt_status(request: Request):
        return JSONResponse({**app.stt.status(), "enabled": app.cfg.get("stt", {}).get("enabled", True)})

    def _voice_settings() -> dict:
        s = app.cfg.get("stt", {})
        return {"read_aloud": s.get("read_aloud", "off"), "wake": app.wake.status(), "stt_enabled": s.get("enabled", True)}

    async def voice_get(request: Request):
        return JSONResponse(_voice_settings())

    async def voice_put(request: Request):
        """{read_aloud?: off|voice|always, wake?: bool}: saved to config.yaml, shared by every chat window."""
        body = await _json(request)
        s = app.cfg.setdefault("stt", {})
        if "read_aloud" in body:
            if body["read_aloud"] not in ("off", "voice", "always"):
                return JSONResponse({"detail": "read_aloud must be off, voice or always"}, status_code=400)
            s["read_aloud"] = body["read_aloud"]
        if "wake" in body:
            if not isinstance(body["wake"], bool):
                return JSONResponse({"detail": "wake must be true or false"}, status_code=400)
            await asyncio.to_thread(app.set_wake, body["wake"])
        persist_cfg()
        out = _voice_settings()
        app.broadcast({"type": "voice", **out})
        return JSONResponse(out)

    async def voice_speaking(request: Request):
        """The page is reading a reply out loud (so "Hey Strux" doesn't hear Strux itself)."""
        body = await _json(request)
        try:
            secs = max(0.0, min(120.0, float(body.get("seconds", 0))))
        except (TypeError, ValueError):
            secs = 0.0
        app.speaking_until = time.time() + secs + (1.0 if secs else 0.0)
        return JSONResponse({"ok": True})

    # ---- controls ----------------------------------------------------------
    async def pause(request: Request):
        body = await _json(request)
        app.set_paused(bool(body.get("paused", not app.paused)))
        return JSONResponse({"paused": app.paused})

    async def forget(request: Request):
        body = await _json(request)
        return JSONResponse(app.forget_minutes(float(body.get("minutes", 10))))

    async def clear_timeline(request: Request):
        body = await _json(request)
        if body.get("confirm") != "DELETE":
            return JSONResponse({"detail": 'send {"confirm": "DELETE"} to clear the whole timeline'}, status_code=400)
        return JSONResponse({"ok": True, **(await asyncio.to_thread(app.clear_timeline))})

    async def think(request: Request):
        app.brain.think_now()
        return JSONResponse({"ok": True})

    async def show(request: Request):
        app.show_window()
        return JSONResponse({"ok": True, "window": app.window is not None})

    async def overlay_show(request: Request):
        if app.overlay:
            app.overlay.summon()
        return JSONResponse({"ok": bool(app.overlay)})

    async def pane_blur(request: Request):
        if app.overlay:
            app.overlay.close_pane(by_blur=True)
        return JSONResponse({"ok": True})

    async def overlay_close(request: Request):
        if app.overlay:
            app.overlay.close()
        return JSONResponse({"ok": True})

    async def overlay_keep(request: Request):
        if app.overlay:
            app.overlay.keep()
        return JSONResponse({"ok": True})

    async def overlay_size(request: Request):
        body = await _json(request)
        if app.overlay and body.get("height"):
            app.overlay.fit(float(body["height"]), float(body["width"]) if body.get("width") else None)
        return JSONResponse({"ok": True})

    async def overlay_expand(request: Request):
        body = await _json(request)
        if app.overlay:
            mode = body.get("mode") or ("panel" if body.get("expanded", True) else "idle")
            app.overlay.expand(mode, body.get("width"), body.get("height"))
        return JSONResponse({"ok": True, "mode": app.overlay.mode if app.overlay else "idle"})

    async def ingest_claude(request: Request):
        body = await _json(request)
        return JSONResponse(app.claude.ingest(body))

    async def claude_state(request: Request):
        return JSONResponse(app.claude.state())

    def st_hot():
        ps = next((x for x in app.sensors if x.name == "process"), None)
        return sorted(set(ps.hot.values())) if ps else []

    def st_top():
        ps = next((x for x in app.sensors if x.name == "process"), None)
        return ps.top[:5] if ps else []

    async def overlay_state(request: Request):
        open_sugg = [dict(r) for r in store.suggestions_page(20) if not r["feedback"]][:5]
        return JSONResponse({"suggestions": open_sugg, "alerts": app.overlay.alerts if app.overlay else [],
                             "chat": app.overlay.chat_log if app.overlay else [], "user": app.user_name, "model": app.brain.llm.model,
                             "attach": app.overlay.attach_log if app.overlay else [],
                             "voice": getattr(app.overlay, "voice_log", []) if app.overlay else [],
                             "read_aloud": app.cfg.get("stt", {}).get("read_aloud", "off"),
                             "processing": app.overlay.processing if app.overlay else "idle", "paused": app.paused,
                             "mode": app.overlay.mode if app.overlay else "idle", "pane": app.overlay.pane if app.overlay else None,
                             "claude": app.claude.state()["sessions"][:3],
                             "hot": st_hot(), "top": st_top(), "chips": app.chips(),
                             "front": dict(zip(("app", "title"), app.foreground()))})

    def _clean_messages(raw) -> list[dict] | None:
        """Validated chat history: [{role: user|assistant, content: str, attachments?: [{name, type, data}]}]."""
        if not isinstance(raw, list):
            return None
        out = []
        for m in raw[-40:]:
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                continue
            atts = m.get("attachments")
            atts = [a for a in atts if isinstance(a, dict) and isinstance(a.get("data"), str)][:6] if isinstance(atts, list) else []
            out.append({"role": m["role"], "content": str(m.get("content") or "")[:40000], "attachments": atts})
        return out if any(m["role"] == "user" for m in out) else None

    async def chat(request: Request):
        try:
            return await _chat(request)
        except Exception as e:                       # never a bare 500 in the chat: say what went wrong
            log.exception("chat failed")
            return JSONResponse({"ok": False, "error": f"Something went wrong ({type(e).__name__}: {str(e)[:160]}). Details in watcher.log."})

    async def _chat(request: Request):
        body = await _json(request)
        msgs = _clean_messages(body.get("messages") if isinstance(body, dict) else None)
        if not msgs:
            return JSONResponse({"detail": "messages required: a list with at least one {role: 'user', content}"}, status_code=400)
        if not await asyncio.to_thread(app.brain.llm.reachable):
            return JSONResponse({"ok": False, "ollama_down": True,
                                 "error": f"Ollama not reachable at {app.brain.llm.base}. Start it and ask again."})
        from ..brain import attachments
        typed = next((m.get("content") or "" for m in reversed(msgs) if m.get("role") == "user"), "")
        try:
            expanded, images, names, problems = await asyncio.to_thread(attachments.expand, msgs)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"couldn't read the attachments: {e}"})
        has_files = any("[Attached file: " in m["content"] for m in expanded)          # images are read in chat.answer
        if names and not images and not has_files:          # everything attached failed to read
            return JSONResponse({"ok": False, "error": "; ".join(problems) or "couldn't read the attachments"})
        changes: list = []
        used: dict = {}
        t0 = time.time()
        text = await asyncio.to_thread(chat_mod.answer, app.brain.llm, store, expanded, None, app.user_name, app.live_context(),
                                       app.planner, changes, images, typed, has_files, used,
                                       knowledge=app.knowledge, screen_ctx={**app.screen_ctx(), "force": body.get("look") is True})
        if text is None:
            return JSONResponse({"ok": False, "error": app.brain.llm.last_error or "chat failed"})
        if problems:
            text += "\n\n(Couldn't read: " + "; ".join(problems) + ")"
        model = used.get("model", app.brain.llm.model)
        latency = round(time.time() - t0, 1)
        scr = used.get("screen") or {}
        mid = None
        conv = body.get("conv")
        if isinstance(conv, str) and conv.strip():
            last = msgs[-1]
            meta = {"model": model, "latency": latency, "changes": changes,
                    "screen": {"app": scr.get("app"), "title": scr.get("title")} if scr else None}
            mid = await asyncio.to_thread(app.knowledge.record_chat, conv.strip(), typed, text, meta,
                                          [a.get("name") for a in last.get("attachments") or []] or None)
        return JSONResponse({"ok": True, "text": text, "latency": latency, "changes": changes, "model": model, "id": mid,
                             "screen": {"thumb": scr.get("thumb"), "app": scr.get("app"), "title": scr.get("title")} if scr else None})

    # ---- chat history + learning ---------------------------------------------
    async def chats(request: Request):
        return JSONResponse({"chats": await asyncio.to_thread(app.knowledge.conversations, int(_q(request, "limit", 15)))})

    async def chat_get(request: Request):
        conv = request.path_params["conv"]
        if request.method == "DELETE":
            return JSONResponse({"ok": True, "deleted": await asyncio.to_thread(app.knowledge.delete_conversation, conv)})
        return JSONResponse({"conv": conv, "messages": await asyncio.to_thread(app.knowledge.conversation, conv)})

    async def chat_rate(request: Request):
        body = await _json(request)
        try:
            mid, rating = int(body.get("id")), int(body.get("rating"))
        except (TypeError, ValueError):
            return JSONResponse({"detail": "id and rating (1, -1 or 0) required"}, status_code=400)
        ok = await asyncio.to_thread(app.knowledge.rate, mid, rating, str(body.get("why") or "")[:300])
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    async def knowledge(request: Request):
        lb = app.learner.block()
        return JSONResponse({**app.knowledge.stats(), "learner": app.learner.state, "cfg": app.cfg.get("learn", {}),
                             "block": {"id": lb["id"], "enabled": lb["enabled"], "lines": len([x for x in lb["content"].splitlines() if x.strip()])} if lb else None})

    async def knowledge_learn(request: Request):
        res = await asyncio.to_thread(app.learner.run, True)
        return JSONResponse(res)

    async def knowledge_clear(request: Request):
        body = await _json(request)
        src = body.get("source")
        if src not in ("claude", "chat", "screen"):
            return JSONResponse({"detail": "source must be claude, chat or screen"}, status_code=400)
        n = await asyncio.to_thread(app.knowledge.clear, src)
        log.info("knowledge cleared: %s (%d notes)", src, n)
        return JSONResponse({"ok": True, "deleted": n})

    async def knowledge_notes(request: Request):
        src = request.query_params.get("source") or None
        q = request.query_params.get("q", "").strip()
        lim = int(_q(request, "limit", 30))
        rows = await asyncio.to_thread(app.knowledge.search, q, src, lim) if q else await asyncio.to_thread(app.knowledge.notes, src, 0, lim)
        return JSONResponse({"notes": [{k: r[k] for k in ("id", "source", "ts", "project", "title", "body", "rating", "extra")} for r in rows]})

    # ---- websocket ----------------------------------------------------------
    async def ws(websocket: WebSocket):
        await websocket.accept()
        hub.clients.add(websocket)
        try:
            await websocket.send_text(json.dumps({"type": "status", **full_status()}, default=str))
            while True:
                await websocket.receive_text()  # keep-alive pings from the client
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            hub.clients.discard(websocket)

    # ---- static UI (SPA) -----------------------------------------------------
    async def spa(request: Request):
        path = request.path_params.get("path", "")
        f = UI_DIST / path
        if path and f.is_file():
            return FileResponse(f)
        # never cache the shell: a stale index.html points at chunk files the last build deleted
        # (WebView2 then runs old code or shows a blank page). Hashed /assets/* stay cacheable.
        return FileResponse(UI_DIST / "index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    async def no_ui(request: Request):
        return PlainTextResponse("UI not built. Run: cd ui && npm install && npm run build")

    routes = [
        Route("/api/status", status),
        Route("/api/timeline", timeline),
        Route("/api/apps", apps),
        Route("/api/frames", frames),
        Route("/api/frames/{frame_id:int}/image", frame_image),
        Route("/api/frames/{frame_id:int}/explain", frame_explain, methods=["POST"]),
        Route("/api/suggestions", suggestions),
        Route("/api/suggestions/test", suggestion_test, methods=["POST"]),
        Route("/api/suggestions/{sid:int}/feedback", feedback, methods=["POST"]),
        Route("/api/search", search),
        Route("/api/summaries", summaries),
        Route("/api/models", models),
        Route("/api/ollama/start", ollama_start, methods=["POST"]),
        Route("/api/settings", get_settings, methods=["GET"]),
        Route("/api/settings", put_settings, methods=["PUT"]),
        Route("/api/control/pause", pause, methods=["POST"]),
        Route("/api/stt/start", stt_start, methods=["POST"]),
        Route("/api/stt/stop", stt_stop, methods=["POST"]),
        Route("/api/stt/cancel", stt_cancel, methods=["POST"]),
        Route("/api/stt/status", stt_status),
        Route("/api/voice", voice_get, methods=["GET"]),
        Route("/api/voice", voice_put, methods=["PUT"]),
        Route("/api/voice/speaking", voice_speaking, methods=["POST"]),
        Route("/api/control/forget", forget, methods=["POST"]),
        Route("/api/control/clear-timeline", clear_timeline, methods=["POST"]),
        Route("/api/control/think", think, methods=["POST"]),
        Route("/api/control/show", show, methods=["POST"]),
        Route("/api/overlay/show", overlay_show, methods=["POST"]),
        Route("/api/overlay/close", overlay_close, methods=["POST"]),
        Route("/api/overlay/keep", overlay_keep, methods=["POST"]),
        Route("/api/overlay/size", overlay_size, methods=["POST"]),
        Route("/api/pane/blur", pane_blur, methods=["POST"]),
        Route("/api/overlay/state", overlay_state),
        Route("/api/overlay/expand", overlay_expand, methods=["POST"]),
        Route("/api/ingest/claude", ingest_claude, methods=["POST"]),
        Route("/api/claude", claude_state),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/chats", chats),
        Route("/api/chats/rate", chat_rate, methods=["POST"]),
        Route("/api/chats/{conv}", chat_get, methods=["GET", "DELETE"]),
        Route("/api/knowledge", knowledge),
        Route("/api/knowledge/learn", knowledge_learn, methods=["POST"]),
        Route("/api/knowledge/clear", knowledge_clear, methods=["POST"]),
        Route("/api/knowledge/notes", knowledge_notes),
        Route("/api/client-error", client_error, methods=["POST"]),
        Route("/api/attach/clipboard", attach_clipboard, methods=["POST"]),
        Route("/api/debug/attach", debug_attach, methods=["POST"]),
        Route("/api/debug/welcome", debug_welcome, methods=["POST"]),
        Route("/api/debug/threads", debug_threads),
        *planner_routes.routes(app, _json, persist_cfg),
        WebSocketRoute("/ws", ws),
    ]
    if UI_DIST.exists():
        routes += [Mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets"),
                   Route("/", spa), Route("/{path:path}", spa)]
    else:
        routes.append(Route("/", no_ui))
    return Starlette(routes=routes, lifespan=lifespan)


class ApiServer:
    def __init__(self, app, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host, self.port = host, port
        self.api = create_api(app)
        cfg = uvicorn.Config(self.api, host=host, port=port, log_level="warning", ws_ping_interval=20, log_config=None)
        self.server = uvicorn.Server(cfg)
        self._t = threading.Thread(target=self.server.run, name="api", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._t.start()
        for _ in range(50):
            if self.server.started:
                break
            time.sleep(0.1)
        log.info("api listening on %s", self.url)

    def stop(self) -> None:
        self.server.should_exit = True
        self._t.join(timeout=3)
