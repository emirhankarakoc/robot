@echo off
cd /d "%~dp0"

title TFM V1.8 - SIMPLE RECORD PLAY

echo ==========================================
echo  TFM V1.8 - LIFECYCLE-SAFE RECORD / SERVER PLAY / AUTOLEARN
echo ==========================================
echo.
echo Commands:
echo   /help
echo   /record on
echo   /record off
echo   /play on
echo   /play off
echo   /recordplayer Nick#0000
echo   /recordplayer off
echo   /playplayer Nick#0000
echo   /playplayer off
echo.
echo No API. No 8787. One Python process.
echo.

py -3.11 main.py

pause
