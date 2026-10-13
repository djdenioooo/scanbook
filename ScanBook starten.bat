@echo off
title ScanBook
rem ScanBook im Browser starten. Diese Datei in den ScanBook-Ordner legen.
cd /d "%~dp0"
if exist "python\python.exe" (
  "python\python.exe" ScanBook_Web.py
) else (
  where py >nul 2>nul && (py -3 ScanBook_Web.py) || (python ScanBook_Web.py)
)
if errorlevel 1 (
  echo.
  echo ScanBook konnte nicht gestartet werden. Ist Python installiert?
  echo Download: https://www.python.org/downloads/  ^(Haken bei "Add python.exe to PATH" setzen^)
  pause
)
