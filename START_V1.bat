@echo off
cd /d "%~dp0"

title TFM V1.2 - SIMPLE RECORD PLAY

echo ==========================================
echo  TFM V1.2 - SIMPLE RECORD / PLAY + LOCAL MIRROR
echo ==========================================
echo.
echo Commands:
echo   /record on
echo   /record off
echo   /play on
echo   /play off
echo.
echo No API. No 8787. One Python process.
echo.

py -3.11 main.py

pause
