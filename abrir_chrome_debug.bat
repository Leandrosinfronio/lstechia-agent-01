@echo off
REM ============================================================
REM  LS.IA Agent 01 - Chrome dedicado em modo debug (porta 9222)
REM  Versao 3: perfil isolado dentro do projeto.
REM  Funciona MESMO com Chrome principal aberto.
REM ============================================================

set "AGENT_DIR=C:\Users\DELL\Documents\AGENT\agent-01"
set "AGENT_PROFILE=%AGENT_DIR%\chrome_agent_profile_real"

echo [1/4] Garantindo que o perfil do agente existe...
if not exist "%AGENT_PROFILE%" mkdir "%AGENT_PROFILE%"

echo [2/4] Removendo locks antigos do perfil do agente...
if exist "%AGENT_PROFILE%\SingletonLock"   del /F /Q "%AGENT_PROFILE%\SingletonLock"   >nul 2>&1
if exist "%AGENT_PROFILE%\SingletonCookie" del /F /Q "%AGENT_PROFILE%\SingletonCookie" >nul 2>&1
if exist "%AGENT_PROFILE%\SingletonSocket" del /F /Q "%AGENT_PROFILE%\SingletonSocket" >nul 2>&1

echo [3/4] Localizando chrome.exe...
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo ERRO: Chrome nao encontrado.
  pause
  exit /b 1
)

echo [4/4] Abrindo Chrome DEDICADO em modo debug na porta 9222...
start "" "%CHROME%" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="%AGENT_PROFILE%" --no-first-run --no-default-browser-check "https://www.linkedin.com/login"

echo.
echo Aguardando o Chrome subir...
timeout /t 6 /nobreak >nul

echo Testando a porta 9222...
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri http://127.0.0.1:9222/json/version -UseBasicParsing -TimeoutSec 5; if ($r.StatusCode -eq 200) { Write-Host 'OK: Chrome em modo debug RESPONDENDO em 127.0.0.1:9222' -ForegroundColor Green } else { Write-Host 'ERRO: Resposta inesperada' -ForegroundColor Red } } catch { Write-Host ('ERRO: ' + $_.Exception.Message) -ForegroundColor Red }"

echo.
echo ============================================================
echo  PRIMEIRA VEZ? Faca login no LinkedIn nesta janela do Chrome.
echo  Esse login fica salvo no perfil do agente PARA SEMPRE.
echo  Voce nao precisa fazer login de novo nas proximas vezes.
echo ============================================================
echo.
echo Depois rode em outro terminal:   .\iniciar_agent_01.bat
echo.
exit /b 0
