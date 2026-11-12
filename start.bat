@echo off
rem FileProbe starten (Datei kann auf diese .bat gezogen werden)
cd /d "%~dp0"
start "" pythonw FileProbe.py %*
