"""Retention job: moves frames down the tiers and enforces the disk cap.

Tier       keeps                          until
hot        OCR text + WebP (q50)          hot_hours (24h)       -> warm
warm       OCR text + 256px thumbnail     warm_days (7d)        -> cold
cold       OCR text only (no pixels)      cold_days (90d)       -> deleted row
dropped    nothing but the row            (low score, demoted early)

Promotion/demotion rules
- score < warm_below_score at the hot->warm boundary  => straight to cold
- score < drop_below_score at the hot->warm boundary  => dropped
- pinned frames (tied to an accepted suggestion) never demote below warm
- disk cap: evict unpinned hot/warm files, lowest score first, oldest first

Runs on the brain's cadence (run_every_minutes) at low priority, and once at
startup. Everything is idempotent.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)


def _thumb(path: Path, size: int = 256) -> Path | None:
    try:
        img = Image.open(path)
        img.thumbnail((size, size))
        out = path.with_name(path.stem + ".thumb.webp")
        img.save(out, "WEBP", quality=40, method=4)
        return out
    except Exception as e:
        log.warning("thumb failed for %s: %s", path, e)
        return None


def _rm(p: str | None) -> None:
    if p:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


def run_once(store, cfg: dict, now: float | None = None) -> dict[str, int]:
    now = now or time.time()
    hot_cut = now - float(cfg.get("hot_hours", 24)) * 3600
    warm_cut = now - float(cfg.get("warm_days", 7)) * 86400
    cold_cut = now - float(cfg.get("cold_days", 90)) * 86400
    warm_below = int(cfg.get("warm_below_score", 30))
    drop_below = int(cfg.get("drop_below_score", 15))
    stats = {"to_warm": 0, "to_cold": 0, "dropped": 0, "deleted": 0, "evicted": 0}

    with store._lock:
        conn = store.conn_for(now)

        # hot -> warm/cold/dropped
        rows = conn.execute("SELECT id,score,pinned,image_path FROM frames WHERE tier='hot' AND ts<?", (hot_cut,)).fetchall()
        for r in rows:
            if not r["pinned"] and r["score"] < drop_below:
                _rm(r["image_path"])
                conn.execute("UPDATE frames SET tier='dropped', image_path=NULL, ocr_text=NULL WHERE id=?", (r["id"],))
                stats["dropped"] += 1
            elif not r["pinned"] and r["score"] < warm_below:
                _rm(r["image_path"])
                conn.execute("UPDATE frames SET tier='cold', image_path=NULL WHERE id=?", (r["id"],))
                stats["to_cold"] += 1
            else:
                t = _thumb(Path(r["image_path"])) if r["image_path"] and Path(r["image_path"]).exists() else None
                _rm(r["image_path"])
                conn.execute("UPDATE frames SET tier='warm', image_path=? WHERE id=?", (str(t) if t else None, r["id"]))
                stats["to_warm"] += 1

        # warm -> cold
        rows = conn.execute("SELECT id,image_path FROM frames WHERE tier='warm' AND ts<?", (warm_cut,)).fetchall()
        for r in rows:
            _rm(r["image_path"])
            conn.execute("UPDATE frames SET tier='cold', image_path=NULL WHERE id=?", (r["id"],))
            stats["to_cold"] += 1

        # cold -> delete rows (text only, old)
        cur = conn.execute("DELETE FROM frames WHERE tier IN ('cold','dropped') AND ts<? AND pinned=0", (cold_cut,))
        stats["deleted"] = cur.rowcount

        # disk cap
        cap = float(cfg.get("disk_cap_mb", 2048)) * 1024 * 1024
        frames_dir = Path(store.data_dir) / "frames"
        used = sum(p.stat().st_size for p in frames_dir.rglob("*.webp")) if frames_dir.exists() else 0
        if used > cap:
            rows = conn.execute(
                "SELECT id,image_path FROM frames WHERE image_path IS NOT NULL AND pinned=0 AND tier IN ('hot','warm') ORDER BY score ASC, ts ASC"
            ).fetchall()
            for r in rows:
                if used <= cap:
                    break
                p = Path(r["image_path"])
                if p.exists():
                    used -= p.stat().st_size
                    _rm(str(p))
                conn.execute("UPDATE frames SET tier='cold', image_path=NULL WHERE id=?", (r["id"],))
                stats["evicted"] += 1

        # remove empty day folders
        if frames_dir.exists():
            for d in frames_dir.iterdir():
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()

    log.info("retention: %s", stats)
    return stats


class RetentionThread:
    def __init__(self, store, cfg: dict) -> None:
        self.store, self.cfg = store, cfg
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, name="retention", daemon=True)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        every = float(self.cfg.get("run_every_minutes", 60)) * 60
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            try:
                run_once(self.store, self.cfg)
            except Exception:
                log.exception("retention failed")
            if self._stop.wait(every):
                return
