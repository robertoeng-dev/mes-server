@echo off
REM Runs the dashboard against the PRODUCTION database (.env),
REM the factory Postgres on the Salcomp corporate network (host set in .env).
set DJANGO_ENV=production
"%~dp0venv\Scripts\python.exe" "%~dp0manage.py" runserver 0.0.0.0:8000
