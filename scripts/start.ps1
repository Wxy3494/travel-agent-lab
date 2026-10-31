param([ValidateSet('api','ui')][string]$Service = 'api')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Create .venv and install dependencies first; see README.md.' }
$taskPort = if ($Service -eq 'api') { 8000 } else { 8501 }
$taskHealthPath = if ($Service -eq 'api') { '/health' } else { '/_stcore/health' }
$taskHealthUrl = "http://127.0.0.1:$taskPort$taskHealthPath"
$taskRunning = $false
try {
    $taskResponse = Invoke-WebRequest -Uri $taskHealthUrl -UseBasicParsing -TimeoutSec 3
    if ($taskResponse.StatusCode -eq 200) {
        if ($Service -eq 'api') {
            $taskHealth = $taskResponse.Content | ConvertFrom-Json
            $taskRunning = $taskHealth.status -eq 'ok' -and $taskHealth.mode -in @('demo','live')
        } else {
            $taskRunning = $taskResponse.Content.Trim() -eq 'ok'
        }
    }
} catch {
    # No responsive service: let the server report any remaining bind/configuration error.
}
if ($taskRunning) {
    Write-Host "$Service is already running on port $taskPort. No duplicate process was started."
    Write-Host 'Open the travel chat in your browser: http://127.0.0.1:8501'
    if ($Service -eq 'api') { Write-Host 'API documentation: http://127.0.0.1:8000/docs' }
    return
}
if ($Service -eq 'api') {
    & $taskPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
} else {
    & $taskPython -m streamlit run app/ui.py --server.address 127.0.0.1 --server.port 8501
}
