"""Background-process sensor. Reads the whole process table every few seconds with one
NtQuerySystemInformation call (winapi.process_snapshot, ~1 ms; psutil opens every process,
which cost ~70 % of Strux's CPU) and turns it into a few meaningful events:

  proc_start   a non-system process appeared and lived >= min_lifetime
  proc_exit    it went away (data.runtime_s, data.cpu_s)
  proc_hot     a process stayed above hot_cpu_percent for `hot_polls` polls
  job_done     a build/test/install style process exited after >= job_min_seconds
               (node, npm, gradle, msbuild, cargo, python, pip, pytest, dotnet, tsc, ...)
  proc_top     periodic snapshot of the top CPU / memory users (for the dashboard)

System noise (svchost, conhost, RuntimeBroker, ...) is ignored.
"""
from __future__ import annotations

import logging
import os
import re
import time

import psutil

from ..events import Event
from . import winapi
from .base import Sensor

log = logging.getLogger(__name__)

SYSTEM_NOISE = {
    "svchost.exe", "conhost.exe", "runtimebroker.exe", "dllhost.exe", "backgroundtaskhost.exe", "wmiprvse.exe",
    "searchprotocolhost.exe", "searchfilterhost.exe", "taskhostw.exe", "sihost.exe", "ctfmon.exe", "dwm.exe",
    "fontdrvhost.exe", "audiodg.exe", "smartscreen.exe", "securityhealthservice.exe", "msmpeng.exe", "mpdefendercoreservice.exe",
    "applicationframehost.exe", "textinputhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
    "msedgewebview2.exe", "crashpad_handler.exe", "wudfhost.exe", "lsass.exe", "csrss.exe", "wininit.exe",
    "services.exe", "winlogon.exe", "system", "registry", "memory compression", "idle", "system idle process",
    "cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "sh.exe", "git.exe", "where.exe", "findstr.exe",
    "wsl.exe", "wslhost.exe", "openconsole.exe", "ollama.exe", "ollama app.exe",
}
JOB_NAMES = {
    "node.exe", "npm.cmd", "npx.cmd", "gradle.exe", "java.exe", "msbuild.exe", "cl.exe", "link.exe", "cargo.exe", "rustc.exe",
    "python.exe", "pip.exe", "pytest.exe", "dotnet.exe", "tsc.exe", "esbuild.exe", "webpack.exe", "vite.exe", "go.exe",
    "gcc.exe", "g++.exe", "clang.exe", "make.exe", "ninja.exe", "cmake.exe", "docker.exe", "ffmpeg.exe", "pyinstaller.exe",
    "7z.exe", "winrar.exe", "adb.exe", "emulator.exe", "eas.cmd", "expo.cmd", "jest.cmd", "vitest.cmd", "mvn.cmd",
}

# long-lived helpers that come and go on their own: their exit is never news
SERVICE_RE = re.compile(r"daemon|language.?server|tsserver|typingsinstaller|vbcscompiler|nodereuse|eslint_d|"
                        r"jdtls|pylance|copilot|kotlin.?compile|esbuild.*service|watchman|crashpad|update|"
                        r"\bagent-[0-9a-f]{8,}", re.I)      # checks a background Claude agent runs in its own worktree


def is_service(label: str | None) -> bool:
    return bool(label and SERVICE_RE.search(label))


def started_by_agent(pid: int) -> bool:
    """Is this process a tool call of Claude Code (some ancestor is claude.exe / the claude-code CLI)?
    Those jobs are Claude's business: its own 'finished' alert covers them."""
    try:
        p = psutil.Process(pid).parent()
        for _ in range(12):
            if p is None:
                return False
            name = p.name().lower()
            if name == "claude.exe":
                return True
            if name == "node.exe" and any("claude-code" in a for a in p.cmdline()[:3]):
                return True
            p = p.parent()
    except (psutil.Error, OSError):
        pass
    return False


def job_label(name: str, cmdline: list[str]) -> str:
    """What a job is, from its command line: 'npm run build in watcher-ai', 'vite dev server in Untrue',
    'pytest tests', 'PyInstaller watcher.spec' instead of just 'node.exe'."""
    import ntpath
    base = name.rsplit(".", 1)[0]
    if "-c" in cmdline[1:3] or "-e" in cmdline[1:3]:
        return f"{base} inline script"
    # words only: drop flags, flag values like 127.0.0.1 / 5173, and inline code
    args = [a for a in cmdline[1:] if a and not a.startswith("-") and "\n" not in a]
    words = lambda xs: [x for x in xs if not any(c in x for c in ".:/\\") and not x.isdigit()]
    if not args:
        return base
    script = ntpath.normpath(args[0].replace("/", "\\"))
    first = ntpath.basename(script).lower()
    parts = script.split("\\")
    low = [x.lower() for x in parts]
    project = parts[low.index("node_modules") - 1] if "node_modules" in low and low.index("node_modules") > 0 else ""
    for tool, label in (("npm-cli.js", "npm"), ("npx-cli.js", "npx"), ("yarn.js", "yarn"), ("pnpm.cjs", "pnpm"), ("pnpm.js", "pnpm")):
        if first == tool:
            return " ".join([label] + words(args[1:3]))
    if first.endswith((".js", ".cjs", ".mjs")) or "node_modules" in low:
        i = low.index("node_modules") if "node_modules" in low else -1
        pkg = parts[i + 1] if 0 <= i < len(parts) - 1 and parts[i + 1] != ".bin" else first.rsplit(".", 1)[0]
        sub = words(args[1:2])
        what = f"{pkg} {sub[0]}" if sub else (f"{pkg} dev server" if pkg in ("vite", "next", "nuxt", "webpack", "astro", "expo") else pkg)
        return f"{what} in {project}" if project and project.lower() not in ("npm", "nodejs") else what
    if "-m" in cmdline[1:3]:
        k = cmdline.index("-m")
        mod = cmdline[k + 1] if k + 1 < len(cmdline) else base
        return " ".join([mod] + words([a for a in cmdline[k + 2:] if not a.startswith("-")])[:1])
    if first.endswith(".py"):
        return f"python {first}"
    return " ".join([base] + [ntpath.basename(a) for a in args[:2]])[:60]


class ProcessSensor(Sensor):
    name = "process"

    def __init__(self, bus, cfg) -> None:
        super().__init__(bus, cfg)
        self._seen: dict[int, dict] = {}      # pid -> {name, create, cpu_hot_polls, parent, announced}
        self._last_top = 0.0
        self.top: list[dict] = []
        self.hot: dict[int, str] = {}
        self._ignore = SYSTEM_NOISE | {n.lower() for n in self.cfg.get("ignore", [])}

    def long_busy(self, minutes: float = 30, min_cpu: float = 10) -> list[dict]:
        """Processes that have kept using >= min_cpu % (smoothed) for >= minutes, busiest first."""
        now = time.time()
        out = []
        for pid, rec in list(self._seen.items()):
            since = rec.get("busy_since")
            if (since and now - since >= minutes * 60 and rec.get("ema", 0) >= min_cpu and not self._noise(rec["name"])
                    and rec["name"] not in ("pythonw.exe", "strux.exe", "halo.exe", "dwm.exe", "explorer.exe")):
                out.append({"pid": pid, "name": rec["name"], "label": rec.get("label"), "cpu": rec["ema"], "seconds": now - since})
        return sorted(out, key=lambda p: -p["cpu"])

    def _noise(self, name: str) -> bool:
        return name in self._ignore

    def run(self) -> None:
        poll = float(self.cfg.get("poll_seconds", 8))
        min_life = float(self.cfg.get("min_lifetime_seconds", 5))
        hot_cpu = float(self.cfg.get("hot_cpu_percent", 25))
        hot_polls = int(self.cfg.get("hot_polls", 3))
        job_min = float(self.cfg.get("job_min_seconds", 20))
        top_every = float(self.cfg.get("top_every_seconds", 30))
        ncpu = os.cpu_count() or 1
        last = time.time()
        for pid, ppid, name, create, cpu, ws in winapi.process_snapshot():     # already running = not news
            self._seen[pid] = {"name": name, "create": create, "hot": 0, "ppid": ppid, "announced": True,
                               "cpu": cpu, "ws": ws, "peak": 0.0}
        while not self.wait(poll):
            if self.paused.is_set():
                continue
            try:
                snap = winapi.process_snapshot()
            except OSError as e:
                log.warning("process snapshot failed: %s", e)
                continue
            now = time.time()
            dt = max(0.5, now - last)
            last = now
            live: set[int] = set()
            rows: list[tuple[float, str, int]] = []
            for pid, ppid, name, create, cpu_t, ws in snap:
                if pid in (0, 4):
                    continue
                live.add(pid)
                rec = self._seen.get(pid)
                if rec is None or abs(rec["create"] - create) > 1:          # new process (or a recycled pid)
                    self._seen[pid] = {"name": name, "create": create or now, "hot": 0, "ppid": ppid, "announced": False,
                                       "cpu": cpu_t, "ws": ws, "peak": 0.0}
                    continue
                cpu = max(0.0, (cpu_t - rec["cpu"]) / 1e7 / dt / ncpu * 100)
                rec["cpu"], rec["ws"] = cpu_t, ws
                if self._noise(name):
                    continue
                rec["peak"] = max(rec["peak"], cpu)
                # smoothed CPU + since when it has stayed busy (for "running for 2 h at 20%")
                rec["ema"] = cpu if "ema" not in rec else rec["ema"] * 0.8 + cpu * 0.2
                if rec["ema"] >= 5:
                    rec.setdefault("busy_since", now)
                elif rec["ema"] < 3:
                    rec.pop("busy_since", None)
                rows.append((cpu, name, pid))
                if not rec["announced"] and now - rec["create"] >= min_life:
                    rec["announced"] = True
                    if name in JOB_NAMES:      # what the job is, read once: 'npm run build', 'pytest tests'
                        try:
                            rec["label"] = job_label(name, psutil.Process(pid).cmdline())
                        except Exception:
                            pass
                        rec["agent"] = started_by_agent(pid)
                    self.emit(Event(kind="proc_start", ts=rec["create"], app=name, data={"pid": pid, "ppid": rec["ppid"]}))
                if cpu >= hot_cpu:
                    rec["hot"] += 1
                    if rec["hot"] == hot_polls:
                        self.hot[pid] = name
                        self.emit(Event(kind="proc_hot", ts=now, app=name, data={"pid": pid, "cpu": round(cpu)}))
                else:
                    rec["hot"] = 0
                    self.hot.pop(pid, None)
            # exits
            for pid in [p for p in self._seen if p not in live]:
                rec = self._seen.pop(pid)
                self.hot.pop(pid, None)
                name = rec["name"]
                if self._noise(name) or not rec["announced"]:
                    continue
                runtime = now - rec["create"]
                data = {"pid": pid, "runtime_s": int(runtime), "peak_cpu": round(rec["peak"])}
                if rec.get("label"):
                    data["label"] = rec["label"]
                if rec.get("agent"):
                    data["agent"] = True
                self.emit(Event(kind="proc_exit", ts=now, app=name, data=data))
                if (name in JOB_NAMES and runtime >= job_min and rec["peak"] >= 5 and not is_service(rec.get("label"))
                        and not rec.get("agent")):
                    self.emit(Event(kind="job_done", ts=now, app=name, data=data))
            # top snapshot
            if now - self._last_top >= top_every:
                self._last_top = now
                rows.sort(reverse=True)
                for _, n, pid in rows[:8]:          # label jobs that were already running when Strux started (once each)
                    rec = self._seen[pid]
                    if n in JOB_NAMES and "label" not in rec:
                        try:
                            rec["label"] = job_label(n, psutil.Process(pid).cmdline())
                        except Exception:
                            rec["label"] = None
                self.top = [{"name": n, "pid": pid, "cpu": round(c, 1), "mem_mb": int(self._seen[pid]["ws"] / 1e6),
                             "label": self._seen[pid].get("label")} for c, n, pid in rows[:8]]
                self.emit(Event(kind="proc_top", ts=now, data={"top": self.top[:5]}))
