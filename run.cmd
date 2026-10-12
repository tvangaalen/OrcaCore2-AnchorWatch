@echo off
rem Starts the Orca Core reader and opens the live page. Extra arguments are passed on (e.g. run.cmd --record).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -m venv .venv && ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)
start "" http://localhost:8765/
".venv\Scripts\python.exe" orca_reader.py %*
