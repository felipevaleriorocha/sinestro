$ErrorActionPreference = 'Stop'
# Inicia o orquestrador (FastAPI + A2A + painel) em 127.0.0.1:<ORCH_PORT, padrão 8787>.
Set-Location $PSScriptRoot

if (!(Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host 'Arquivo .env criado a partir do .env.example. Edite PROJECT_ROOT e OPENCODE_SERVER_PASSWORD e execute novamente.'
    exit 0
}
if (!(Test-Path '.venv')) { py -3 -m venv .venv }
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

# Reinstala dependências somente quando requirements.txt muda.
$stamp = Join-Path $PSScriptRoot '.venv\.requirements.sha256'
$hash = (Get-FileHash 'requirements.txt' -Algorithm SHA256).Hash
if (!(Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $hash) {
    & $python -m pip install --upgrade pip | Out-Null
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar dependências.' }
    Set-Content $stamp $hash
}

& $python run_server.py
