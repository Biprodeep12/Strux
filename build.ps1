# Build a portable folder: dist\Strux\Strux.exe
# Native tools log to stderr; PowerShell 5.1 would turn that into an error when output is redirected,
# so success is decided by exit codes, not by stderr.
$ErrorActionPreference = "Continue"
Push-Location ui
npm run build
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { Write-Error "UI build failed ($code)"; exit $code }
.\.venv\Scripts\python -m PyInstaller --noconfirm --clean watcher.spec 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller failed ($LASTEXITCODE)"; exit $LASTEXITCODE }
Write-Host "Built: dist\Strux\Strux.exe"
