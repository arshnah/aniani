@echo off
REM Windows launcher. Unverified on real Windows -- built via platform_utils.py's
REM documented conventions, needs real testing.
cd /d "%~dp0"
python gui.py
