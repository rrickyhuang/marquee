@echo off
cd /d "%~dp0"
echo Running watchlist checker...
python watchlist_checker.py
echo.
pause
