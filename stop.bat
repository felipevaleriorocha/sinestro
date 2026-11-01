@echo off
setlocal

rem Usa a pasta onde este .bat está localizado.
set "ORCH=%~dp0"

echo [Sinestro] Encerrando servicos em segundo plano...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ORCH%scripts\stop_all.ps1"

timeout /t 2 /nobreak >nul
endlocal
