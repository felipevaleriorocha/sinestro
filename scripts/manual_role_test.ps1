param(
    [Parameter(Mandatory)][ValidateSet('deepseek', 'gemini')][string]$Implementer
)
# TESTE REAL (CONSOME COTA): um agente implementa e o outro revisa, num workspace de rascunho (não no jogo).
# Requer o OpenCode rodando (.\run_opencode.ps1). NÃO precisa do orquestrador rodando.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (!(Test-Path $python)) { throw 'Ambiente virtual não encontrado. Execute .\start.ps1 uma vez antes.' }
Write-Warning "Este teste envia prompts reais ao Gemini e ao DeepSeek (consome cota)."
& $python -m tools.manual_role_test --implementer $Implementer
exit $LASTEXITCODE
