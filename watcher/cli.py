"""Command line: run, timeline, stats, suggestions, feedback, retention, llm-test."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

from . import config as config_mod
from .store import Store


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _already_running(cfg: dict) -> bool:
    """If an instance is already serving the API, ask it to show its window and return True."""
    import urllib.request
    base = f"http://{cfg['server']['host']}:{cfg['server']['port']}"
    try:
        urllib.request.urlopen(f"{base}/api/status", timeout=1).read()
    except Exception:
        return False
    try:
        req = urllib.request.Request(f"{base}/api/control/show", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass
    return True


def cmd_run(args) -> None:
    from .app import App
    cfg = config_mod.load(args.config)
    if cfg["server"].get("enabled", True) and _already_running(cfg):
        print("Strux is already running; brought its window to front.")
        return
    if getattr(args, "hidden", False):
        cfg["ui"]["start_hidden"] = True
    App(cfg).run_forever(window=not getattr(args, "no_window", False))


def cmd_open(args) -> None:
    import os
    cfg = config_mod.load(args.config)
    os.startfile(f"http://{cfg['server']['host']}:{cfg['server']['port']}")


def cmd_timeline(args) -> None:
    cfg = config_mod.load(args.config)
    store = Store(cfg["data_dir"])
    events = store.recent_events(args.minutes)
    cur_app, since = "", None
    for ev in events:
        if ev["kind"] == "window":
            if cur_app and since is not None and ev["app"] != cur_app:
                print(f"{_fmt(since)} - {_fmt(ev['ts'])}  {cur_app:<22} {int(ev['ts'] - since):>5}s")
                since = ev["ts"]
            elif since is None:
                since = ev["ts"]
            cur_app = ev["app"]
            if args.verbose:
                print(f"    {_fmt(ev['ts'])} -> {ev['title'][:90]}")
        elif ev["kind"] == "title" and args.verbose:
            print(f"    {_fmt(ev['ts'])}    {ev['title'][:90]}")
        elif ev["kind"] in ("idle_start", "idle_end", "clipboard", "blocked"):
            d = json.loads(ev["data"] or "{}")
            extra = d.get("idle_seconds") or d.get("preview", "")[:40] or d.get("reason", "")
            print(f"    {_fmt(ev['ts'])} [{ev['kind']}] {extra}")
    if cur_app and since is not None:
        print(f"{_fmt(since)} - now       {cur_app:<22} {int(time.time() - since):>5}s")
    frames = store.recent_frames(args.minutes)
    if frames:
        print(f"\nframes: {len(frames)}  (avg score {sum(f['score'] for f in frames) / len(frames):.0f})")
        for f in frames[-10:]:
            print(f"    {_fmt(f['ts'])} {f['tier']:<5} score {f['score']:>3} {f['app']:<18} {f['title'][:50]}")


def cmd_stats(args) -> None:
    cfg = config_mod.load(args.config)
    print(json.dumps(Store(cfg["data_dir"]).stats(), indent=2))


def cmd_suggestions(args) -> None:
    cfg = config_mod.load(args.config)
    for r in Store(cfg["data_dir"]).recent_suggestions(args.minutes):
        print(f"#{r['id']:<4} {_fmt(r['ts'])} [{r['source']}] {r['feedback'] or '-':<8} {r['text']}")


def cmd_feedback(args) -> None:
    cfg = config_mod.load(args.config)
    ok = Store(cfg["data_dir"]).set_feedback(args.id, args.value)
    print("ok" if ok else "no such suggestion")


def cmd_retention(args) -> None:
    from .retention import run_once
    cfg = config_mod.load(args.config)
    print(run_once(Store(cfg["data_dir"]), cfg["retention"]))


def cmd_llm_test(args) -> None:
    from .brain.llm import LLM, GpuGuard, system_busy
    from .brain import summarizer
    cfg = config_mod.load(args.config)
    llm = LLM(cfg["brain"]["llm"])
    print("endpoint:", llm.base, "model:", llm.model, "reachable:", llm.reachable())
    print("gpu:", GpuGuard().status(), "busy:", system_busy(cfg["brain"], GpuGuard()))
    win = summarizer.build(Store(cfg["data_dir"]), minutes=15)
    print("--- summary ---\n" + win.as_text())
    if llm.reachable():
        t0 = time.time()
        print("--- answer ---\n", repr(llm.suggest(win.as_text())), f"({time.time() - t0:.1f}s)")


def main(argv=None) -> None:
    import os, sys
    # pythonw.exe / windowed exe: no console streams; give libraries something to write to
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    p = argparse.ArgumentParser(prog="watcher", description="Strux - local desktop watcher + suggester")
    p.add_argument("--config", default=None, help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="start sensors, brain, API, tray and dashboard window (default)")
    r.add_argument("--no-window", action="store_true", help="tray + API only; open the dashboard in a browser")
    r.add_argument("--hidden", action="store_true", help="start minimised to the tray (used by the logon task)")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("open", help="open the dashboard URL in the default browser").set_defaults(fn=cmd_open)
    t = sub.add_parser("timeline", help="print recent activity")
    t.add_argument("--minutes", type=float, default=60)
    t.add_argument("-v", "--verbose", action="store_true")
    t.set_defaults(fn=cmd_timeline)
    sub.add_parser("stats", help="store statistics").set_defaults(fn=cmd_stats)
    s = sub.add_parser("suggestions", help="list suggestions")
    s.add_argument("--minutes", type=float, default=24 * 60)
    s.set_defaults(fn=cmd_suggestions)
    f = sub.add_parser("feedback", help="mark a suggestion accept|dismiss|snooze")
    f.add_argument("id", type=int)
    f.add_argument("value", choices=["accept", "dismiss", "snooze"])
    f.set_defaults(fn=cmd_feedback)
    sub.add_parser("retention", help="run the retention job now").set_defaults(fn=cmd_retention)
    sub.add_parser("llm-test", help="check Ollama + ask once").set_defaults(fn=cmd_llm_test)
    args = p.parse_args(argv)
    if not args.cmd:
        args.fn = cmd_run
    args.fn(args)
