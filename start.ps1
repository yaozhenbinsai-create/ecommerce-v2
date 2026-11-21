$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw '请先按 README 创建 .venv 并安装依赖。' }
Push-Location -LiteralPath $PSScriptRoot
try { & $projectPython run.py; exit $LASTEXITCODE }
finally { Pop-Location }
