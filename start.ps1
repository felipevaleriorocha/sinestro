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

# Inicia o Rojo Serve automaticamente em background se o projeto Roblox tiver default.project.json
$rojoProcess = $null
try {
    $projectRootLine = (Get-Content '.env' -ErrorAction SilentlyContinue | Where-Object { $_ -match '^\s*PROJECT_ROOT\s*=\s*(.+)$' } | Select-Object -First 1)
    if ($projectRootLine) {
        $projectRoot = $projectRootLine -replace '^\s*PROJECT_ROOT\s*=\s*', ''
        $projectRoot = $projectRoot.Trim().Trim('"' + "'")
        if ($projectRoot -and (Test-Path (Join-Path $projectRoot 'default.project.json'))) {
            $portInUse = $false
            try {
                $tcp = New-Object System.Net.Sockets.TcpClient
                $connect = $tcp.BeginConnect('127.0.0.1', 34872, $null, $null)
                $wait = $connect.AsyncWaitHandle.WaitOne(500, $false)
                if ($wait -and $tcp.Connected) { $portInUse = $true }
                $tcp.Close()
            } catch { }

            if (-not $portInUse) {
                Write-Host "Iniciando Rojo serve automaticamente em: $projectRoot" -ForegroundColor Cyan
                $rojoProcess = Start-Process -FilePath "rojo" -ArgumentList "serve" -WorkingDirectory $projectRoot -PassThru -WindowStyle Hidden
            } else {
                Write-Host "Rojo serve já está ativo na porta 34872." -ForegroundColor Green
            }
        }
    }
} catch {
    Write-Warning "Aviso ao verificar Rojo: $_"
}

try {
    & $python run_server.py
} finally {
    if ($rojoProcess -and -not $rojoProcess.HasExited) {
        Write-Host "Encerrando processo do Rojo em background..." -ForegroundColor Yellow
        Stop-Process -Id $rojoProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
