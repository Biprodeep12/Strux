# Start Watcher AI (tray + dashboard window). Run from the project folder.
$ErrorActionPreference = "Stop"
if (-not (Test-Path ".venv")) {
  py -V:Astral/CPython3.12.12 -m venv .venv
  .\.venv\Scripts\python -m pip install -r requirements.txt
}
if (-not (Test-Path "ui\dist\index.html")) {
  Push-Location ui
  npm install --no-audit --no-fund
  npm run build
  Pop-Location
}
.\.venv\Scripts\python -m watcher run @args
