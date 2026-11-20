@echo off
rem Vela launcher: uses the Python on PATH and this directory as PYTHONPATH.
setlocal
set "VELA_HOME=%~dp0"
set "PYTHONPATH=%VELA_HOME%;%PYTHONPATH%"
python -m vela %*
exit /b %ERRORLEVEL%
