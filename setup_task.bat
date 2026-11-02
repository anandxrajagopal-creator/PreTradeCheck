@echo off
REM Registers PreTradeCheck in Task Scheduler under the \ProTrader\ folder.
REM Runs Sun-Thu at 8:30 PM (the evening before each trading day), headless via pythonw.exe.
REM Edit PYEXE / SCRIPT if your paths differ, then right-click -> Run as administrator.

set PYEXE=C:\Users\ANAND R\PycharmProjects\PreTradeCheck\.venv\Scripts\pythonw.exe
set SCRIPT=C:\Users\ANAND R\PycharmProjects\PreTradeCheck\PreTradeCheck.py

schtasks /create /tn "\ProTrader\ProTrader_PreTradeCheck" ^
  /tr "\"%PYEXE%\" \"%SCRIPT%\"" ^
  /sc weekly /d SUN,MON,TUE,WED,THU /st 20:30 /f

REM remove the old task name if it exists
schtasks /delete /tn "\ProTrader\PreTradeCheck" /f >nul 2>&1

echo.
echo Done. Test it now with:  schtasks /run /tn "\ProTrader\ProTrader_PreTradeCheck"
pause
