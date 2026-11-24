@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo gateway.ini 의 서버 주소와 토큰으로 연결을 확인합니다.
EUM-Gateway.exe --check
echo.
pause
