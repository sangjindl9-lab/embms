@echo off
chcp 65001 >nul
cd /d "%~dp0"
schtasks /delete /tn "EUM BMS Gateway" /f
pause
