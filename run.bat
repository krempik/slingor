@echo off
setlocal EnableExtensions
title SLINGOR — запуск
cd /d "%~dp0"

rem ---------------------------------------------------------- venv python
set "PY=%CD%..\4ayka-kit\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   ============================================================
echo     SLINGOR.IO — игра живёт на http://127.0.0.1:8033
echo     (открывается в любом браузере: Chrome, Firefox, Edge, ...)
echo   ============================================================
echo.

rem -------------------------------------------------- server is running?
netstat -ano | findstr /C:":8033 " | findstr /C:"LISTENING" >nul
if not errorlevel 1 goto server_ok

echo   Сервер не запущен — поднимаю...
start "SLINGOR-SERVER" "%PY%" main.py

set /a tries=0
:wait_port
netstat -ano | findstr /C:":8033 " | findstr /C:"LISTENING" >nul
if not errorlevel 1 goto server_ok
set /a tries+=1
if %tries% GEQ 30 (
  echo.
  echo   ! Сервер не поднялся за 30 секунд. Загляните в окно SLINGOR-SERVER.
  echo   ! Если там "Address already in use" - значит сервер уже работает.
  pause
  goto :open_browser
)
timeout /t 1 /nobreak >nul
goto wait_port

:server_ok
echo   Сервер: запущен.
echo.

:open_browser
echo   Открываю игру в браузере по умолчанию...
start "" "http://127.0.0.1:8033"

echo.
echo   УПРАВЛЕНИЕ:  WASD — тяга    МЫШЬ/джойстик — маневры
echo   ЗАКРЫТЬ ЭТО ОКНО МОЖНО СРАЗУ — сервер не остановится.
echo.
pause
exit /b 0