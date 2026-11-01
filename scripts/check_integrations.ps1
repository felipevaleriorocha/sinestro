param(
    [switch]$Orchestrator,  # também consulta o orquestrador em execução e os Agent Cards A2A
    [switch]$Json
)
# Verifica Antigravity (agy), OpenCode, modelo, contrato de rotas, quota, checagens e MCP SEM consumir tokens.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (!(Test-Path $python)) { throw 'Ambiente virtual não encontrado. Execute .\start.ps1 uma vez antes.' }

$argsList = @('-m', 'tools.check_integrations')
if ($Orchestrator) { $argsList += '--orchestrator' }
if ($Json) { $argsList += '--json' }
& $python @argsList
exit $LASTEXITCODE
