@echo off
cd /d "%~dp0"

title emirhankarakoc v1.4

echo ==============================================================
echo  emirhankarakoc v1.4 - RECORD / RACING / AUTOLEARN
echo ==============================================================
echo.
echo Commands:
echo   /help
echo   /record on ^| off
echo   /play on ^| off
echo   /afkfarming on ^| off
echo   /recordplayer Nick#0000 ^| off
echo   /playplayer Nick#0000 ^| off
echo   /timelist [@mapCode]
echo   /timedelete [@mapCode^|all]
echo   /timeowner @mapCode Nick#0000
echo   /blacklist add/remove/list/clear [Nick#0000]
echo.
echo BEST only. Minimum record time: 11.000 seconds.
echo.

py -3.11 main.py

pause
