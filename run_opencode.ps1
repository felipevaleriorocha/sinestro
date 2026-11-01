$ErrorActionPreference = 'Stop'
# Inicia o servidor HTTP do OpenCode 2.x no workspace do jogo, restrito a 127.0.0.1.
. (Join-Path $PSScriptRoot 'scripts\EnvFile.ps1')

$envFile = Join-Path $PSScriptRoot '.env'
if (!(Test-Path $envFile)) { throw 'Crie .env copiando .env.example e configure PROJECT_ROOT.' }
$settings = Read-EnvFile $envFile

if ([string]::IsNullOrWhiteSpace($settings['PROJECT_ROOT'])) { throw 'PROJECT_ROOT não definido no .env.' }
$project = [Environment]::ExpandEnvironmentVariables($settings['PROJECT_ROOT'])
if (!(Test-Path $project -PathType Container)) { throw "Pasta do projeto não encontrada: $project" }

$port = 4096
if ($settings['OPENCODE_URL'] -match ':(\d+)/?$') { $port = [int]$Matches[1] }

$env:OPENCODE_SERVER_USERNAME = if ($settings['OPENCODE_SERVER_USERNAME']) { $settings['OPENCODE_SERVER_USERNAME'] } else { 'opencode' }
if ($settings['OPENCODE_SERVER_PASSWORD']) {
    $env:OPENCODE_SERVER_PASSWORD = $settings['OPENCODE_SERVER_PASSWORD']
} else {
    Write-Warning 'OPENCODE_SERVER_PASSWORD vazio: o servidor ficará sem autenticação.'
}

$opencode = Get-Command opencode.cmd -ErrorAction SilentlyContinue
if (-not $opencode) { $opencode = Get-Command opencode.exe -ErrorAction SilentlyContinue }
if (-not $opencode) { throw 'opencode.cmd/opencode.exe não encontrado no PATH.' }

Set-Location $project
Write-Host "Iniciando OpenCode em: $project"
Write-Host "Servidor restrito a 127.0.0.1:$port (usuário: $($env:OPENCODE_SERVER_USERNAME); senha: oculta)."
& $opencode.Source serve --hostname 127.0.0.1 --port $port
