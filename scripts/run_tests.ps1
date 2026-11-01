param(
    [switch]$Live,        # inclui testes reais SEM tokens (agy --help, /api/info, /api/model, /api/agent)
    [switch]$LiveTokens   # inclui testes reais que enviam prompts (consome cota)
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (!(Test-Path $python)) { throw 'Ambiente virtual não encontrado. Execute .\start.ps1 uma vez antes.' }
& $python -m pip install -q -r requirements-dev.txt

Write-Host '== Testes com MOCKS (não provam integração real) =='
& $python -m pytest tests/unit -q
$unit = $LASTEXITCODE

if ($Live -or $LiveTokens) {
    Write-Host '== Testes REAIS =='
    $env:RUN_LIVE_TESTS = '1'
    if ($LiveTokens) { $env:RUN_LIVE_TOKEN_TESTS = '1' }
    & $python -m pytest tests/live -v -s
    $live = $LASTEXITCODE
    Remove-Item Env:RUN_LIVE_TESTS, Env:RUN_LIVE_TOKEN_TESTS -ErrorAction SilentlyContinue
    if ($live -ne 0) { exit $live }
}
exit $unit
