@echo off
setlocal

rem Usa a pasta onde este .bat está localizado.
set "ORCH=%~dp0"

if not exist "%ORCH%start.ps1" (
    echo Arquivo start.ps1 nao encontrado em: %ORCH%
    pause
    exit /b 1
)

echo [Sinestro] Iniciando OpenCode e Orquestrador em segundo plano (headless)...
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%ORCH%scripts\start_headless.ps1"

endlocal
