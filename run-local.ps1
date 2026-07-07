# run-local.ps1 — start SanctionPay backend + frontend on your machine
# Usage:  powershell -ExecutionPolicy Bypass -File .\run-local.ps1
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# First-run: create venv + install backend deps if missing
if (-not (Test-Path "$root\backend\.venv\Scripts\python.exe")) {
    Write-Host "First run: creating venv and installing backend dependencies..."
    python -m venv "$root\backend\.venv"
    & "$root\backend\.venv\Scripts\python.exe" -m pip install --upgrade pip
    & "$root\backend\.venv\Scripts\python.exe" -m pip install -r "$root\backend\requirements.txt"
}
if (-not (Test-Path "$root\.env")) { Copy-Item "$root\.env.example" "$root\.env" }

# Backend (FastAPI) on :8000 — loads real OFAC/UN sanction lists in the background
Start-Process -FilePath "$root\backend\.venv\Scripts\python.exe" `
    -ArgumentList '-m','uvicorn','main:app','--host','127.0.0.1','--port','8000' `
    -WorkingDirectory "$root\backend"

# Frontend (static) on :3000
Start-Process -FilePath "python" `
    -ArgumentList '-m','http.server','3000','--bind','127.0.0.1','--directory',"$root\frontend"

Write-Host ""
Write-Host "SanctionPay is live:"
Write-Host "  Frontend   http://localhost:3000"
Write-Host "  API docs   http://localhost:8000/docs"
Write-Host "  Stats      http://localhost:8000/stats"
Write-Host ""
Write-Host "Close the two spawned windows (or Stop-Process on the python PIDs) to stop."
