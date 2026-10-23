"""Windows toast notifications via winotify (no COM registration needed)."""
from __future__ import annotations

import logging

from ..events import Suggestion

log = logging.getLogger(__name__)
from .. import APP_NAME

APP_ID = APP_NAME


def show(s: Suggestion, launch: str | None = None) -> None:
    try:
        from winotify import Notification, audio
        n = Notification(app_id=APP_ID, title=f"Suggestion #{s.id or ''} ({s.source})", msg=s.text, duration="long",
                         launch=(launch + "/#/suggestions") if launch else "")
        n.set_audio(audio.Silent, loop=False)
        n.show()
    except Exception as e:
        log.warning("toast failed: %s", e)


def info(title: str, msg: str) -> None:
    try:
        from winotify import Notification, audio
        n = Notification(app_id=APP_ID, title=title, msg=msg, duration="short")
        n.set_audio(audio.Silent, loop=False)
        n.show()
    except Exception as e:
        log.warning("toast failed: %s", e)
