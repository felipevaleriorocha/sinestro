$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (!(Test-Path '.env')) { Copy-Item '.env.example' '.env'; Write-Host 'Arquivo .env criado. Edite PROJECT_ROOT e execute novamente.'; exit 0 }
if (!(Test-Path '.venv')) { py -3 -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8787
