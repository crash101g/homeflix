@echo off
rem Double-click to install Homeflix on Windows. Options are passed to install.ps1, e.g.  install.cmd -Service
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
rem Keep the window open when started by double-click
echo %cmdcmdline% | find /i "%~0" >nul && pause
