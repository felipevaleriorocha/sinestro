$ErrorActionPreference = 'Stop'
$envFile = Join-Path $PSScriptRoot '.env'
if (!(Test-Path $envFile)) { throw 'Crie .env copiando .env.example e configure PROJECT_ROOT.' }

# Lê somente as variáveis necessárias para localizar o workspace e autenticar o servidor.
$settings = Get-Content $envFile | Where-Object {
    $_ -match '^[A-Za-z_][A-Za-z0-9_]*='
} | ConvertFrom-StringData

$project = [Environment]::ExpandEnvironmentVariables($settings.PROJECT_ROOT.Trim().Trim('"'))
if (!(Test-Path $project -PathType Container)) { throw "Pasta do projeto não encontrada: $project" }

if ($settings.OPENCODE_SERVER_USERNAME) {
    $env:OPENCODE_SERVER_USERNAME = $settings.OPENCODE_SERVER_USERNAME
}
if ($settings.OPENCODE_SERVER_PASSWORD) {
    $env:OPENCODE_SERVER_PASSWORD = $settings.OPENCODE_SERVER_PASSWORD
}

Set-Location $project
Write-Host "Iniciando OpenCode 2.x em: $project"
Write-Host 'Servidor restrito a 127.0.0.1:4096.'
opencode.cmd serve --hostname 127.0.0.1 --port 4096
