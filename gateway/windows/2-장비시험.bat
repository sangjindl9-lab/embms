@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 서버 없이 장비를 한 번 읽어 봅니다.
set /p PORT=COM 포트 (예: COM3) 또는 장비 IP: 
set TPL=pzem016
set /p TPL=장비 템플릿 [pzem016 / sdm120] (엔터=pzem016): 
set UNIT=1
set /p UNIT=국번 (엔터=1): 
echo.
EUM-Gateway.exe --test %PORT% --template %TPL% --unit %UNIT%
echo.
pause
