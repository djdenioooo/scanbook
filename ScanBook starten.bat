@echo off
title ScanBook
rem ScanBook im Browser starten. Diese Datei in den ScanBook-Ordner legen.
cd /d "%~dp0"
where py >/dev/null 2>/dev/null && (py -3 ScanBook_Web.py) || (python ScanBook_Web.py)
if errorlevel 1 (
  echo.
  echo ScanBook konnte nicht gestartet werden. Ist Python installiert?
  echo Download: https://www.python.org/downloads/  ^(Haken bei "Add python.exe to PATH" setzen^)
  pause
)
