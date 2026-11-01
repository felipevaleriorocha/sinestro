@echo off
setlocal
rem Copie este arquivo para a Area de Trabalho. Ele usa a pasta abaixo, nao a pasta onde o .bat esta.
set "ORCH=C:\Users\Felipe\Downloads\roblox_orchestrator_starter"

if not exist "%ORCH%\start.ps1" (
    echo Pasta do orquestrador nao encontrada: %ORCH%
    pause
    exit /b 1
)

echo Iniciando servidor OpenCode...
start "OpenCode (DeepSeek)" powershell -NoExit -ExecutionPolicy Bypass -File "%ORCH%\run_opencode.ps1"

echo Aguardando o OpenCode subir...
timeout /t 5 /nobreak >nul

echo Iniciando orquestrador...
start "Orquestrador" powershell -NoExit -ExecutionPolicy Bypass -File "%ORCH%\start.ps1"

echo Aguardando o orquestrador subir...
timeout /t 8 /nobreak >nul
start "" "http://127.0.0.1:8787/dashboard/"

endlocal
