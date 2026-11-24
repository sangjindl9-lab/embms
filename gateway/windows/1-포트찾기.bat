@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [연결된 COM 포트]
EUM-Gateway.exe --ports
echo.
echo USB-RS485 컨버터(CH340 등)가 보이면 그 COM 번호를 적어 두세요.
pause
