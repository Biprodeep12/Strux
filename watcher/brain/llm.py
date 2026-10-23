"""Local LLM client + resource guards.

- Ollama native /api/chat is used so `keep_alive: 0` unloads the model from
  VRAM right after each answer. Any OpenAI-compatible server also works
  (provider: openai) for e.g. llama.cpp server or LM Studio.
- Guards: skip the call entirely if the GPU is busy (you are gaming / training)
  or there is not enough free VRAM, or the CPU is pegged.
- stdlib HTTP only: the `requests` package alone costs ~11 MB of resident memory.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Callable

import psutil

from . import prompts

log = logging.getLogger(__name__)

# prompts live in brain/prompts.py (editable from Settings)


def _http(method: str, url: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


class GpuGuard:
    def __init__(self) -> None:
        self._nvml = None
        self._handle = None
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception as e:
            log.info("NVML unavailable (%s) - GPU guard disabled", e)
        self.own_mb: Callable[[], int] = lambda: 0   # VRAM Strux itself holds (resident whisper): not "someone else busy"

    def status(self) -> dict | None:
        if not self._nvml:
            return None
        try:
            util = self._nvml.nvmlDeviceGetUtilizationRates(self._handle).gpu
            mem = self._nvml.nvmlDeviceGetMemoryInfo(self._handle)
            own = int(self.own_mb() or 0)
            return {"util": int(util), "free_mb": int(mem.free // (1024 * 1024)) + own, "total_mb": int(mem.total // (1024 * 1024)),
                    "own_mb": own}
        except Exception:
            return None


def system_busy(cfg: dict, gpu: GpuGuard) -> str | None:
    """Return a reason string if the machine is busy, else None."""
    cpu = psutil.cpu_percent(interval=None)
    if cpu > float(cfg.get("max_cpu_percent", 60)):
        return f"cpu {cpu:.0f}%"
    llm = cfg.get("llm", {})
    st = gpu.status()
    if st:
        if st["util"] > int(llm.get("max_gpu_util_percent", 30)):
            return f"gpu {st['util']}%"
        if st["free_mb"] < int(llm.get("min_free_vram_mb", 4800)):
            return f"vram free {st['free_mb']}MB"
    return None


def fit_messages(messages: list[dict], ctx_tokens: int, reserve: int) -> list[dict]:
    """Trim a chat so it fits `ctx_tokens` (~3.2 chars / token, conservative for code and Indic text):
    drop the oldest non-system turns first, then shorten the longest remaining message in the middle."""
    budget = max(512, ctx_tokens - reserve) * 3.2
    msgs = [dict(m) for m in messages]
    size = lambda: sum(len(str(m.get("content") or "")) for m in msgs)
    while size() > budget and sum(1 for m in msgs if m.get("role") != "system") > 1:
        i = next(i for i, m in enumerate(msgs) if m.get("role") != "system")
        if msgs[i].get("role") == "tool" or i == len(msgs) - 1:
            break
        msgs.pop(i)
    while size() > budget:
        i = max(range(len(msgs)), key=lambda k: len(str(msgs[k].get("content") or "")))
        c = str(msgs[i].get("content") or "")
        over = int(size() - budget) + 200
        if len(c) <= over + 400:
            msgs[i]["content"] = c[: max(200, len(c) - over)]
            if len(c) - over < 200:
                break
        else:
            keep = (len(c) - over) // 2
            msgs[i]["content"] = c[:keep] + "\n[… shortened to fit the model …]\n" + c[-keep:]
    return msgs


def _ollama_exe() -> tuple[list[str], str] | None:
    """How to start Ollama here: its tray app (which runs the server), else `ollama serve`."""
    import os
    import shutil
    from pathlib import Path
    home = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"
    if (home / "ollama app.exe").is_file():
        return [str(home / "ollama app.exe")], "ollama app"
    exe = shutil.which("ollama") or (str(home / "ollama.exe") if (home / "ollama.exe").is_file() else None)
    return ([exe, "serve"], "ollama serve") if exe else None


_starting = threading.Lock()


def start_ollama(llm: "LLM", wait_s: float = 30) -> dict:
    with _starting:                                  # two clicks / windows: one start, the second sees it up
        return _start_ollama(llm, wait_s)


def _start_ollama(llm: "LLM", wait_s: float) -> dict:
    """Bring a local Ollama up: a server that stopped answering (e.g. after a GPU crash) is killed first, then the
    app is launched outside Strux's process tree (so stopping Strux leaves it running) and polled until it answers."""
    import subprocess
    from urllib.parse import urlparse
    if llm.reachable():
        return {"ok": True, "started": False, "note": "Ollama is already running"}
    if llm.provider != "ollama" or urlparse(llm.base).hostname not in ("localhost", "127.0.0.1", "::1"):
        return {"ok": False, "error": f"the model server at {llm.base} isn't a local Ollama; start it there"}
    how = _ollama_exe()
    if not how:
        return {"ok": False, "error": "Ollama isn't installed (no 'ollama app.exe' or 'ollama' on PATH)"}
    stale = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() in ("ollama.exe", "ollama app.exe")]
    for p in stale:
        try:
            p.kill()
        except Exception:
            pass
    psutil.wait_procs(stale, timeout=5)
    flags = 0x00000008 | 0x00000200 | 0x08000000      # DETACHED_PROCESS | NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    try:
        try:
            subprocess.Popen(how[0], creationflags=flags | 0x01000000, close_fds=True)   # + BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(how[0], creationflags=flags, close_fds=True)              # the job doesn't allow breakaway
    except OSError as e:
        return {"ok": False, "error": f"couldn't start {how[1]}: {e}"}
    log.info("starting Ollama (%s)%s", how[1], f", killed {len(stale)} stuck process(es)" if stale else "")
    t0 = time.time()
    while time.time() - t0 < wait_s:
        time.sleep(0.5)
        if llm.reachable():
            llm.last_error = None
            log.info("Ollama up in %.1fs", time.time() - t0)
            return {"ok": True, "started": True, "restarted": bool(stale), "seconds": round(time.time() - t0, 1)}
    return {"ok": False, "error": f"started {how[1]} but it didn't answer within {wait_s:.0f} s"}


class LLM:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.provider = cfg.get("provider", "ollama")
        self.base = cfg.get("base_url", "http://localhost:11434").rstrip("/")
        self.model = cfg.get("model", "qwen2.5:7b-instruct-q4_K_M")
        self.timeout = float(cfg.get("timeout_seconds", 90))
        self.last_error: str | None = None
        self.last_latency: float = 0.0
        self._busy = 0                               # model requests in flight (parallel chats)

    def reachable(self) -> bool:
        try:
            url = f"{self.base}/api/tags" if self.provider == "ollama" else f"{self.base}/models"
            _http("GET", url, timeout=2)
            return True
        except Exception:
            return False

    def models(self) -> list[str]:
        """Installed model names (Ollama only)."""
        try:
            r = _http("GET", f"{self.base}/api/tags", timeout=3)
            return [m["name"] for m in r.get("models", [])]
        except Exception:
            return []

    def chat_tools(self, messages: list[dict], tools: list[dict], max_tokens: int = 400, temperature: float = 0.1) -> dict | None:
        self._busy += 1
        try:
            return self._chat_tools(messages, tools, max_tokens, temperature)
        finally:
            self._busy -= 1

    def _chat_tools(self, messages: list[dict], tools: list[dict], max_tokens: int, temperature: float) -> dict | None:
        """One chat turn that may call tools. Returns {"content", "tool_calls": [{"name", "arguments": dict}],
        "message": raw assistant message to append to the history}, or None on failure."""
        t0 = time.time()
        try:
            ctx = int(self.cfg.get("chat_num_ctx", 6144))
            messages = fit_messages(messages, ctx // (2 if self._busy > 1 else 1), max_tokens + 1200)   # tools schema ~1k tokens
            if self.provider == "ollama":
                r = _http("POST", f"{self.base}/api/chat", {
                    "model": self.model, "stream": False, "tools": tools,
                    "keep_alive": self.cfg.get("chat_keep_alive", "3m"),
                    "options": {"num_ctx": int(self.cfg.get("chat_num_ctx", 6144)), "temperature": temperature, "num_predict": max_tokens},
                    "messages": messages,
                }, timeout=self.timeout * 2)
                msg = r["message"]
                calls = [{"name": c["function"]["name"], "arguments": c["function"].get("arguments") or {}}
                         for c in msg.get("tool_calls") or []]
            else:
                r = _http("POST", f"{self.base}/chat/completions", {
                    "model": self.model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages, "tools": tools,
                }, timeout=self.timeout * 2)
                msg = r["choices"][0]["message"]
                calls = []
                for c in msg.get("tool_calls") or []:
                    args = c["function"].get("arguments") or "{}"
                    calls.append({"name": c["function"]["name"], "arguments": json.loads(args) if isinstance(args, str) else args})
        except Exception as e:
            self.last_error = str(e)
            log.warning("llm tool chat failed: %s", e)
            return None
        self.last_latency = time.time() - t0
        self.last_error = None
        for c in calls:          # some models return arguments as a JSON string even on Ollama
            if isinstance(c["arguments"], str):
                try:
                    c["arguments"] = json.loads(c["arguments"])
                except ValueError:
                    c["arguments"] = {}
        return {"content": (msg.get("content") or "").strip(), "tool_calls": calls, "message": msg}

    def _chat_raw(self, messages: list[dict], *, model: str, max_tokens: int, temperature: float, timeout: float,
                  keep_alive=None, options: dict | None = None) -> str:
        ctx = int((options or {}).get("num_ctx") or self.cfg.get("num_ctx", 4096))
        # several requests at once share the model's context in Ollama: budget for half when another one may run
        messages = fit_messages(messages, ctx // (2 if self._busy > 1 else 1), max_tokens + 64)
        if self.provider == "ollama":
            r = _http("POST", f"{self.base}/api/chat", {
                "model": model, "stream": False,
                "keep_alive": self.cfg.get("keep_alive", 0) if keep_alive is None else keep_alive,
                "options": {"num_ctx": int(self.cfg.get("num_ctx", 4096)), "temperature": temperature, "num_predict": max_tokens,
                            **(options or {})},
                "messages": messages,
            }, timeout=timeout)
            return r["message"]["content"]
        r = _http("POST", f"{self.base}/chat/completions",
                  {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}, timeout=timeout)
        return r["choices"][0]["message"]["content"]

    def suggest(self, summary_text: str) -> str | None:
        """Returns suggestion text, '' for NONE, or None on failure."""
        t0 = time.time()
        try:
            text = self._chat_raw(
                [{"role": "system", "content": prompts.with_memory(prompts.get("suggest"))}, {"role": "user", "content": summary_text}],
                model=self.model, max_tokens=60, temperature=0.3, timeout=self.timeout,
            )
        except Exception as e:
            self.last_error = str(e)
            log.warning("llm call failed: %s", e)
            return None
        self.last_latency = time.time() - t0
        self.last_error = None
        text = (text or "").strip().splitlines()[0].strip(" \"'*-") if (text or "").strip() else ""
        if not text or text.upper().startswith("NONE") or len(text) < 12:
            return ""
        return text[:200]

    def chat(self, messages: list[dict], max_tokens: int = 400, temperature: float = 0.4,
             model: str | None = None, images: list[str] | None = None, options: dict | None = None,
             keep_alive=None) -> str | None:
        self._busy += 1
        try:
            return self._chat(messages, max_tokens, temperature, model, images, options, keep_alive)
        finally:
            self._busy -= 1

    def _chat(self, messages, max_tokens, temperature, model, images, options, keep_alive) -> str | None:
        """General chat. `images` (base64 list) attaches to the last user message (vision models)."""
        t0 = time.time()
        model = model or self.model
        msgs = [dict(m) for m in messages]
        if images and msgs:
            if self.provider == "ollama":
                msgs[-1]["images"] = images
            else:
                msgs[-1]["content"] = [{"type": "text", "text": msgs[-1]["content"]}] + [
                    {"type": "image_url", "image_url": {"url": f"data:image/webp;base64,{b}"}} for b in images]
        try:
            text = self._chat_raw(msgs, model=model, max_tokens=max_tokens, temperature=temperature, timeout=self.timeout * 2,
                                  keep_alive=self.cfg.get("chat_keep_alive", "3m") if keep_alive is None else keep_alive,
                                  options=options)
        except Exception as e:
            self.last_error = str(e)
            log.warning("llm chat failed: %s", e)
            return None
        self.last_latency = time.time() - t0
        self.last_error = None
        return (text or "").strip()
