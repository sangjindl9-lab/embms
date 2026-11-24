@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 게이트웨이를 실행합니다. 이 창을 닫으면 수집이 멈춥니다. (종료: Ctrl+C)
EUM-Gateway.exe
pause
