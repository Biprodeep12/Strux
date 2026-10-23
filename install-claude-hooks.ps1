# Register (or remove) the Watcher AI hooks in ~/.claude/settings.json.
# Merges with whatever hooks already exist; never replaces the file.
# Usage:  .\install-claude-hooks.ps1            .\install-claude-hooks.ps1 -Remove
param([switch]$Remove)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = (Join-Path $root "hooks\claude_hook.py") -replace "\\", "/"
$settings = Join-Path $env:USERPROFILE ".claude\settings.json"
$py = @"
import json, pathlib, sys
p = pathlib.Path(r'''$settings''')
script = r'''$script'''
remove = $(if ($Remove) { "True" } else { "False" })
events = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SubagentStop"]
d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
hooks = d.setdefault("hooks", {})
def is_ours(h): return h.get("command") == "py" and len(h.get("args", [])) >= 3 and h["args"][2].replace("\\", "/").lower() == script.lower()
for ev in events:
    groups = hooks.get(ev, [])
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if not is_ours(h)]
    groups = [g for g in groups if g.get("hooks")]
    if not remove:
        groups.append({"hooks": [{"type": "command", "command": "py", "args": ["-3", "-I", script, ev], "timeout": 5, "async": True}]})
    if groups: hooks[ev] = groups
    else: hooks.pop(ev, None)
p.write_text(json.dumps(d, indent=2), encoding="utf-8")
print(("removed" if remove else "installed") + " Watcher AI hooks for: " + ", ".join(events))
"@
py -3 -I -c $py
Write-Host "Open /hooks in Claude Code once (or restart it) to reload the configuration."
