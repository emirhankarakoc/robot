@echo off
cd /d "%~dp0"

title emirhankarakoc v1.10

echo ==============================================================
echo  emirhankarakoc v1.10 - RECORD / RACING / AUTOLEARN
echo ==============================================================
echo.
echo Commands:
echo   /help
echo   /record on ^| off
echo   /play on ^| off
echo   /sismanlattrambolin on [W] [H] [gorunmezacik^|gorunmezkapali] ^| off
echo   /sismanlatlav on [W] [H] [gorunmezacik^|gorunmezkapali] ^| off
echo   /debuglogs on ^| off
echo   /chatafterfirst on ^| off ^| [message]
echo   /afkfarming on ^| off
echo   /recordplayer Nick#0000 ^| off
echo   /playplayer Nick#0000 ^| off
echo   /timelist              - all maps, MIRRORED YES/NO
echo   /timelist @mapCode     - one map, MIRRORED YES/NO
echo   /timedelete [ID^|@mapCode^|all]
echo   /timeowner @mapCode Nick#0000
echo   /blacklist add/remove/list/clear [Nick#0000]
echo.
echo BEST only. Minimum record time: 8.000 seconds.
echo.

py -3.11 main.py

pause
