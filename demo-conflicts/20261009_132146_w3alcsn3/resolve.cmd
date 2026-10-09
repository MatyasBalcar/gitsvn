@echo off
python "D:\Patches\GitSVN\main.py" --config "%~dp0config.json" resolve %*
exit /b %errorlevel%
