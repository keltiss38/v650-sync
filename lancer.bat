@echo off
rem Lance la page web du V650 (installe l'environnement Python local au premier lancement).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Premiere utilisation : installation de l'environnement, environ une minute...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)
".venv\Scripts\python.exe" -m v650sync ui
pause
