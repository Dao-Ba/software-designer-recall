@echo off
cd /d "%~dp0"
start /b python scripts\server.py --open
exit