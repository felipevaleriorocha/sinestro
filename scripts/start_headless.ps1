$ErrorActionPreference = 'SilentlyContinue'
$PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $PSScriptRoot

Set-Location $projectRoot

function Test-PortListening([int]$port) {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect('127.0.0.1', $port, $null, $null)
        $wait = $iar.AsyncWaitHandle.WaitOne(400, $false)
        if ($wait -and $tcp.Connected) {
            $tcp.Close()
            return $true
        }
        $tcp.Close()
    } catch {}
    return $false
}

# 1. Inicia OpenCode se não estiver ativo na porta 4096
if (-not (Test-PortListening 4096)) {
    $opencodeScript = Join-Path $projectRoot 'run_opencode.ps1'
    if (Test-Path $opencodeScript) {
        Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$opencodeScript`"" -WindowStyle Hidden
    }
}

# 2. Inicia Orquestrador se não estiver ativo na porta 8787
if (-not (Test-PortListening 8787)) {
    $startScript = Join-Path $projectRoot 'start.ps1'
    if (Test-Path $startScript) {
        Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`"" -WindowStyle Hidden
    }
}

# 3. Aguarda o orquestrador responder no endpoint /health
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        $res = Invoke-WebRequest -Uri 'http://127.0.0.1:8787/health' -UseBasicParsing -TimeoutSec 1
        if ($res.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {}
    Start-Sleep -Milliseconds 500
}

# 4. Abre o dashboard no navegador padrão
if ($ready) {
    Start-Process 'http://127.0.0.1:8787/dashboard/'
}
