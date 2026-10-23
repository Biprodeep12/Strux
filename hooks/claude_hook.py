"""Claude Code hook -> Watcher AI bridge.

Claude Code runs this for every hook event with the event JSON on stdin.
We forward it to the local Watcher API and ALWAYS exit 0 within ~1 s so a
stopped Watcher can never slow Claude down. stdlib only, no venv needed.

Installed in ~/.claude/settings.json (see install-claude-hooks.ps1).
"""
import json
import sys
import urllib.request

URL = "http://127.0.0.1:8765/api/ingest/claude"


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    if len(sys.argv) > 1 and "hook_event_name" not in data:
        data["hook_event_name"] = sys.argv[1]
    try:
        req = urllib.request.Request(URL, data=json.dumps(data).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=1.0).read()
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
