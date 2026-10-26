@echo off
setlocal
set "PYTHONPATH=%~dp0"
set "PYTHONUTF8=1"
pushd "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m perfmon.cli %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
