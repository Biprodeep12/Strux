"""Configuration loading. Defaults live here; config.yaml overrides them."""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
FROZEN = bool(getattr(sys, "frozen", False))
# Packaged exe: keep user data + editable config in %LOCALAPPDATA%\WatcherAI, never inside the install folder.
USER_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Strux" if FROZEN else ROOT

DEFAULTS: dict[str, Any] = {
    "data_dir": str(USER_DIR / "data"),
    "log_level": "INFO",
    "priority": {"below_normal": True, "eco_qos": True},
    "sensors": {
        "window": {"enabled": True},
        "browser": {"enabled": True},
        "process": {"enabled": True, "poll_seconds": 8, "min_lifetime_seconds": 5, "hot_cpu_percent": 25,
                    "hot_polls": 3, "job_min_seconds": 20, "top_every_seconds": 30, "ignore": []},
        "idle": {"enabled": True, "poll_seconds": 5, "idle_after_seconds": 120},
        "clipboard": {"enabled": True, "poll_seconds": 2, "preview_chars": 200},
        "screenshot": {
            "enabled": True,
            "interval_seconds": 15,
            "on_window_change": True,
            "min_gap_seconds": 3,
            "video_interval_seconds": 90,
            "phash_min_distance": 6,
            "max_width": 1280,
            "hot_quality": 50,
            "ocr": False,
            "ocr_min_gap_seconds": 20,
            "ocr_max_width": 1280,
            "blocklist_apps": [
                "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe",
                "lastpass.exe", "zoom.exe", "teams.exe", "ms-teams.exe",
            ],
            "blocklist_title_keywords": [
                "incognito", "inprivate", "private browsing", "bank", "password",
                "otp", "payment", "checkout",
            ],
        },
    },
    "retention": {
        "hot_hours": 24,
        "warm_days": 7,
        "cold_days": 90,
        "warm_below_score": 30,
        "drop_below_score": 15,
        "disk_cap_mb": 2048,
        "run_every_minutes": 60,
    },
    "server": {"enabled": True, "host": "127.0.0.1", "port": 8765},
    # speech to text in the chats (watcher/stt.py): local faster-whisper, on the GPU when there is one (CPU fallback);
    # models download once to data/models
    "stt": {"enabled": True, "device": "auto", "model": "small", "gpu_model": "large-v3-turbo", "language": "en", "beam_size": 5, "max_seconds": 120, "unload_minutes": 5,
            "read_aloud": "off",          # read replies out: off | voice (when you spoke) | always
            # "Hey Strux" (watcher/wake.py): keeps the mic open, so off until you switch it on
            "wake": {"enabled": False, "sound": True, "silence_ms": 1200, "listen_timeout_s": 6, "max_command_s": 30}},
    "user": {"name": ""},
    # "what's on my screen" (brain/screen.py): OCR only unless vision is on (the vision model has crashed the GPU driver here)
    "screen": {"vision": False},
    # what Strux learns (watcher/knowledge.py + brain/learner.py), all local in data/knowledge.db
    "learn": {"enabled": True,
              "claude": True,             # read Claude Code transcripts (~/.claude/projects) for what you asked + its answers
              "backfill_days": 14,        # how far back the first read goes
              "sync_minutes": 5,
              "screens": True,            # keep a note of each "what's on my screen" look
              "recall": True,             # put related past notes into chat answers
              "profile": True,            # the 'Learned' Memory block, rewritten every `every_hours`
              "every_hours": 3},
    "prompts": {},                 # overrides of brain/prompts.py DEFAULTS (Settings -> AI prompts); empty = default          # how Strux addresses you; empty = first name of the Windows account
    "brain": {
        "never_suggest_apps": [],
        "tick_seconds": 30,
        "info_expire_minutes": 15,     # heads-ups with nothing to do leave Activity by themselves
        "max_cpu_percent": 60,
        "global_cooldown_minutes": 6,
        "snooze_minutes": 30,
        "quiet_when_idle": True,
        "llm": {
            "enabled": True,
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "model": "qwen2.5:7b-instruct-q4_K_M",
            "vision_model": "qwen2.5vl:3b",
            "interval_minutes": 5,
            "num_ctx": 4096,
            "timeout_seconds": 180,
            "keep_alive": 0,
            "chat_keep_alive": "3m",
            "max_gpu_util_percent": 30,
            "min_free_vram_mb": 4800,
        },
        "rules": {
            "app_switch_storm": {"enabled": True, "window_minutes": 5, "min_switches": 25, "cooldown_minutes": 30},
            "long_focus": {"enabled": True, "minutes": 50, "cooldown_minutes": 60},
            "error_on_screen": {"enabled": True, "cooldown_minutes": 15},
            "clipboard_repeat": {"enabled": True, "window_minutes": 3, "min_copies": 8, "cooldown_minutes": 30},
            "welcome_back": {"enabled": True, "min_idle_minutes": 10, "cooldown_minutes": 5},
            "job_done": {"enabled": True, "cooldown_minutes": 2, "min_minutes": 3},   # yours only, 3+ min
            "todo_due": {"enabled": True, "lead_minutes": 30, "cooldown_minutes": 2},
            "event_soon": {"enabled": True, "lead_minutes": 15, "cooldown_minutes": 2},
            "morning_plan": {"enabled": True, "from_hour": 5, "until_hour": 13, "cooldown_minutes": 60},
            "wrap_up": {"enabled": True, "hour": 21, "cooldown_minutes": 60},
            "busy_process": {"enabled": True, "minutes": 30, "min_cpu": 10, "cooldown_minutes": 60},
            "revisit_page": {"enabled": True, "min_visits": 4, "cooldown_minutes": 45},
            "distraction": {"enabled": True, "window_minutes": 45, "minutes": 25, "cooldown_minutes": 60},
        },
    },
    "ui": {"toasts": True, "tray": True, "window": True, "start_hidden": False,
           "overlay": {"enabled": True, "always_visible": True, "timeout_seconds": 14}},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def config_path() -> Path:
    """Where config.yaml is read from and written to (user dir when frozen)."""
    return USER_DIR / "config.yaml"


def load(path: str | Path | None = None) -> dict:
    """Load config.yaml (if present) merged over DEFAULTS."""
    cfg_path = Path(path) if path else config_path()
    if FROZEN and not cfg_path.exists() and (ROOT / "config.yaml").exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    user_cfg: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
    cfg = deep_merge(DEFAULTS, user_cfg)
    Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
    (Path(cfg["data_dir"]) / "frames").mkdir(parents=True, exist_ok=True)
    return cfg
