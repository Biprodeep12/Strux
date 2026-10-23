"""TODO / calendar / plan / prompt routes.

  GET    /api/todos?done=1            open TODOs (+ done in the last 7 days with done=1)
  POST   /api/todos                   {text} quick-add ("Fix login tomorrow 10am !high #proj") or {title, due, priority, project, notes}
  PATCH  /api/todos/{id}              {title?, due?, priority?, project?, notes?, done?}
  DELETE /api/todos/{id}
  GET    /api/todos/{id}/files        files kept with a TODO
  POST   /api/todos/{id}/files?name=  raw file bytes as the body (Content-Type = the file's type), <= 25 MB
  GET    /api/todo-files/{fid}        the file (inline; ?download=1 to save)
  POST   /api/todo-files/{fid}/open   open it in its default app on this PC (programs / scripts: show in folder instead)
  DELETE /api/todo-files/{fid}
  GET    /api/calendar?start=&end=    events overlapping [start, end) (epoch seconds; default: this month ±1 week)
  POST   /api/calendar                {title, start, end?, all_day?, location?, notes?}   (times: epoch or text)
  PATCH  /api/calendar/{id}           same fields
  DELETE /api/calendar/{id}
  POST   /api/plan                    "Plan my day" from TODOs + calendar + recent work
  GET    /api/prompts                 every model prompt: default, your override, what it is used for
  PUT    /api/prompts                 {key: "text" | "" | null}   "" / null = back to default
  GET    /api/memory                  Memory blocks (what the AI knows about you)
  POST   /api/memory                  {title, content?}      PATCH /api/memory/{id} {title?, content?, enabled?}
  DELETE /api/memory/{id}
  GET    /api/memory/preview?q=       exactly what the chat model would get for question q (no model call)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..brain import chat as chat_mod
from ..brain import prompts
from ..planner import parse_when, quick_parse

PRIO = {"low": 0, "normal": 1, "high": 2}
# opening these would run them: "Open" shows them in Explorer instead
RUNNABLE = {".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".msp", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
            ".wsh", ".hta", ".lnk", ".url", ".jar", ".reg", ".cpl", ".pif", ".appref-ms", ".msc", ".scf", ".inf", ".application"}


def _prio(v: Any) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, str) and v.lower() in PRIO:
        return PRIO[v.lower()]
    try:
        return max(0, min(2, int(v)))
    except (TypeError, ValueError):
        return None


def routes(app, read_json: Callable, persist_cfg: Callable[[], None]) -> list[Route]:
    planner = app.planner

    def bad(msg: str, code: int = 400) -> JSONResponse:
        return JSONResponse({"detail": msg}, status_code=code)

    # ---- todos -------------------------------------------------------------------------------
    async def todos_list(request: Request):
        done = request.query_params.get("done") in ("1", "true")
        return JSONResponse({"todos": planner.todos(include_done=done, done_since=time.time() - 7 * 86400)})

    async def todos_add(request: Request):
        b = await read_json(request)
        if b.get("text") is not None and not isinstance(b.get("text"), str):
            return bad("text must be a string")
        if b.get("text"):
            q = quick_parse(b["text"])
            if not q["title"]:
                return bad("empty title")
            t = planner.add_todo(q["title"], due_ts=q["due_ts"], priority=q["priority"], project=q["project"],
                                 notes=str(b.get("notes", "")), source=str(b.get("source", "you")))
            t.pop("_existing", None)
            return JSONResponse({"todo": t})
        if not str(b.get("title", "")).strip():
            return bad("title or text required")
        due = parse_when(b.get("due")) if b.get("due") not in (None, "") else None
        if b.get("due") not in (None, "") and due is None:
            return bad(f"could not understand due '{b.get('due')}'")
        t = planner.add_todo(str(b["title"]), due_ts=due, priority=_prio(b.get("priority")) if _prio(b.get("priority")) is not None else 1,
                             project=str(b.get("project", "")), notes=str(b.get("notes", "")), source=str(b.get("source", "you")))
        t.pop("_existing", None)
        return JSONResponse({"todo": t})

    async def todos_patch(request: Request):
        b = await read_json(request)
        ch: dict[str, Any] = {k: b[k] for k in ("title", "project", "notes") if k in b}
        if "priority" in b and _prio(b["priority"]) is not None:
            ch["priority"] = _prio(b["priority"])
        if "due" in b:
            if b["due"] in (None, ""):
                ch["due_ts"] = None
            else:
                ch["due_ts"] = parse_when(b["due"])
                if ch["due_ts"] is None:
                    return bad(f"could not understand due '{b['due']}'")
        if "done" in b:
            ch["done"] = bool(b["done"])
        t = planner.update_todo(int(request.path_params["tid"]), **ch)
        return JSONResponse({"todo": t}) if t else bad("not found", 404)

    async def todos_delete(request: Request):
        return JSONResponse({"ok": True}) if planner.delete_todo(int(request.path_params["tid"])) else bad("not found", 404)

    # ---- files under a TODO ----------------------------------------------------------------------
    async def files_list(request: Request):
        return JSONResponse({"files": planner.files(int(request.path_params["tid"]))})

    async def files_add(request: Request):
        from ..planner import MAX_FILE_BYTES
        tid = int(request.path_params["tid"])
        if int(request.headers.get("content-length") or 0) > MAX_FILE_BYTES:
            return bad("file is larger than 25 MB", 413)
        data = await request.body()
        mime = (request.headers.get("content-type") or "").split(";")[0]
        try:
            f = await asyncio.to_thread(planner.add_file, tid, request.query_params.get("name") or "file", data, mime)
        except KeyError:
            return bad("TODO not found", 404)
        return JSONResponse({"file": f})

    async def file_get(request: Request):
        from starlette.responses import FileResponse
        f = planner.file(int(request.path_params["fid"]))
        path = planner.file_path(f) if f else None
        if not path or not path.is_file():
            return bad("not found", 404)
        inline = request.query_params.get("download") not in ("1", "true")
        return FileResponse(path, media_type=f["mime"] or None, filename=f["name"],
                            content_disposition_type="inline" if inline else "attachment",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def file_open(request: Request):
        import os
        import subprocess
        f = planner.file(int(request.path_params["fid"]))
        path = planner.file_path(f) if f else None
        if not path or not path.is_file():
            return bad("not found", 404)
        if path.suffix.lower() in RUNNABLE:          # never run a program from here: show it in Explorer instead
            subprocess.Popen(["explorer", "/select,", str(path)])
            return JSONResponse({"ok": True, "shown_in_folder": True})
        os.startfile(str(path))
        return JSONResponse({"ok": True})

    async def file_delete(request: Request):
        return JSONResponse({"ok": True}) if planner.delete_file(int(request.path_params["fid"])) else bad("not found", 404)

    # ---- calendar ------------------------------------------------------------------------------
    async def cal_list(request: Request):
        now = datetime.now()
        d0 = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)
        try:
            start = float(request.query_params.get("start", d0.timestamp()))
            end = float(request.query_params.get("end", (d0 + timedelta(days=45)).timestamp()))
        except ValueError:
            return bad("start / end must be epoch seconds")
        return JSONResponse({"events": planner.events_between(start, end)})

    def _times(b: dict, need_start: bool) -> tuple[dict, str | None]:
        out: dict[str, Any] = {}
        for k in ("start", "end"):
            if b.get(k) not in (None, ""):
                v = parse_when(b[k])
                if v is None:
                    return {}, f"could not understand {k} '{b[k]}'"
                out[f"{k}_ts"] = v
        if need_start and "start_ts" not in out:
            return {}, "start required"
        if "start_ts" in out and "end_ts" in out and out["end_ts"] < out["start_ts"]:
            return {}, "end is before start"
        return out, None

    async def cal_add(request: Request):
        b = await read_json(request)
        if not str(b.get("title", "")).strip():
            return bad("title required")
        tm, err = _times(b, True)
        if err:
            return bad(err)
        e = planner.add_event(str(b["title"]), tm["start_ts"], tm.get("end_ts"), all_day=bool(b.get("all_day")),
                              location=str(b.get("location", "")), notes=str(b.get("notes", "")), source=str(b.get("source", "you")))
        return JSONResponse({"event": e})

    async def cal_patch(request: Request):
        b = await read_json(request)
        tm, err = _times(b, False)
        if err:
            return bad(err)
        ch = {**tm, **{k: b[k] for k in ("title", "location", "notes") if k in b}}
        if "all_day" in b:
            ch["all_day"] = int(bool(b["all_day"]))
        e = planner.update_event(int(request.path_params["eid"]), **ch)
        return JSONResponse({"event": e}) if e else bad("not found", 404)

    async def cal_delete(request: Request):
        return JSONResponse({"ok": True}) if planner.delete_event(int(request.path_params["eid"])) else bad("not found", 404)

    # ---- AI ----------------------------------------------------------------------------------------
    async def plan(request: Request):
        llm = app.brain.llm
        if not await asyncio.to_thread(llm.reachable):
            return JSONResponse({"ok": False, "error": f"LLM not reachable at {llm.base}."})
        text = await asyncio.to_thread(chat_mod.plan, llm, app.store, planner, app.user_name)
        return JSONResponse({"ok": bool(text), "text": text, "error": None if text else (llm.last_error or "failed"),
                             "model": llm.model, "latency": round(llm.last_latency, 1)})

    async def prompts_get(request: Request):
        return JSONResponse({"prompts": prompts.listing()})

    async def prompts_put(request: Request):
        b = await read_json(request)
        unknown = [k for k in b if k not in prompts.DEFAULTS]
        if unknown:
            return bad(f"unknown prompt(s): {', '.join(unknown)}")
        over = dict(app.cfg.get("prompts") or {})
        for k, v in b.items():
            if v is not None and not isinstance(v, str):
                return bad(f"{k}: a prompt must be text (or null to reset)")
            if v is None or not v.strip():
                over.pop(k, None)
            else:
                over[k] = v[:20000]
        app.cfg["prompts"] = over
        persist_cfg()
        return JSONResponse({"ok": True, "prompts": prompts.listing()})

    import functools
    import sqlite3

    def safe(fn):
        @functools.wraps(fn)
        async def wrapped(request: Request):
            try:
                return await fn(request)
            except (ValueError, TypeError, OverflowError, KeyError, sqlite3.IntegrityError) as e:
                return bad(f"invalid input: {e}")
        return wrapped

    # ---- memory -----------------------------------------------------------------------------------------------
    memory = app.memory

    async def mem_list(request: Request):
        return JSONResponse({"blocks": memory.blocks(), "context": memory.context_text()})

    async def mem_add(request: Request):
        b = await read_json(request)
        return JSONResponse({"block": memory.add(b.get("title"), b.get("content") or "")})

    async def mem_patch(request: Request):
        b = await read_json(request)
        blk = memory.update(int(request.path_params["bid"]), title=b.get("title"), content=b.get("content"),
                            enabled=b.get("enabled"))
        return JSONResponse({"block": blk}) if blk else bad("not found", 404)

    async def mem_delete(request: Request):
        return JSONResponse({"ok": True}) if memory.delete(int(request.path_params["bid"])) else bad("not found", 404)

    async def mem_remember(request: Request):
        b = await read_json(request)
        blk, added = memory.remember(b.get("fact"), b.get("block") or "Notes")
        return JSONResponse({"block": blk, "added": added})

    async def mem_preview(request: Request):
        q = request.query_params.get("q") or "What did I do today?"
        system = chat_mod.system_prompt(app.user_name)
        kn = getattr(app, "knowledge", None)
        hint = kn.feedback_hint(q) if kn is not None else ""
        system = prompts.with_memory(system + ("\n\n" + hint if hint else ""))
        ctx = await asyncio.to_thread(chat_mod.build_context, app.store, q, None, None, "", planner, kn)
        return JSONResponse({"question": q, "system": system, "context": ctx, "chars": len(system) + len(ctx)})

    mem_list, mem_add, mem_patch, mem_delete, mem_preview, mem_remember = map(
        safe, (mem_list, mem_add, mem_patch, mem_delete, mem_preview, mem_remember))
    todos_list, todos_add, todos_patch, todos_delete = map(safe, (todos_list, todos_add, todos_patch, todos_delete))
    files_list, files_add, file_get, file_open, file_delete = map(safe, (files_list, files_add, file_get, file_open, file_delete))
    cal_list, cal_add, cal_patch, cal_delete, plan, prompts_put = map(safe, (cal_list, cal_add, cal_patch, cal_delete, plan, prompts_put))
    return [
        Route("/api/todos", todos_list, methods=["GET"]),
        Route("/api/todos", todos_add, methods=["POST"]),
        Route("/api/todos/{tid:int}", todos_patch, methods=["PATCH"]),
        Route("/api/todos/{tid:int}", todos_delete, methods=["DELETE"]),
        Route("/api/todos/{tid:int}/files", files_list, methods=["GET"]),
        Route("/api/todos/{tid:int}/files", files_add, methods=["POST"]),
        Route("/api/todo-files/{fid:int}", file_get, methods=["GET"]),
        Route("/api/todo-files/{fid:int}/open", file_open, methods=["POST"]),
        Route("/api/todo-files/{fid:int}", file_delete, methods=["DELETE"]),
        Route("/api/calendar", cal_list, methods=["GET"]),
        Route("/api/calendar", cal_add, methods=["POST"]),
        Route("/api/calendar/{eid:int}", cal_patch, methods=["PATCH"]),
        Route("/api/calendar/{eid:int}", cal_delete, methods=["DELETE"]),
        Route("/api/plan", plan, methods=["POST"]),
        Route("/api/prompts", prompts_get, methods=["GET"]),
        Route("/api/memory", mem_list, methods=["GET"]),
        Route("/api/memory", mem_add, methods=["POST"]),
        Route("/api/memory/preview", mem_preview, methods=["GET"]),
        Route("/api/memory/remember", mem_remember, methods=["POST"]),
        Route("/api/memory/{bid:int}", mem_patch, methods=["PATCH"]),
        Route("/api/memory/{bid:int}", mem_delete, methods=["DELETE"]),
        Route("/api/prompts", prompts_put, methods=["PUT"]),
    ]
