@echo off
setlocal
set "ORCH=%~dp0"

echo [Sinestro] Reiniciando servicos (Orquestrador, OpenCode, Rojo)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ORCH%scripts\stop_all.ps1"
timeout /t 1 /nobreak >nul

echo [Sinestro] Iniciando servicos em segundo plano (headless)...
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%ORCH%scripts\start_headless.ps1"

endlocal
