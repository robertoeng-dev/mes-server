@echo off
REM Runs the dashboard against the LOCAL/DEV database (.env.local).
REM Never touches the Salcomp production database.
set DJANGO_ENV=local
"%~dp0venv\Scripts\python.exe" "%~dp0manage.py" runserver
