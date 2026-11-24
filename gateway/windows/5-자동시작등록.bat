@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Windows 로그인 시 게이트웨이가 자동으로 실행되도록 등록합니다.
schtasks /create /tn "EUM BMS Gateway" /tr "\"%~dp0EUM-Gateway.exe\"" /sc onlogon /f
if errorlevel 1 (echo 등록 실패: 이 파일을 마우스 오른쪽 - 관리자 권한으로 실행 해 보세요.) else (echo 등록 완료. 다음 로그인부터 자동 실행됩니다.)
pause
