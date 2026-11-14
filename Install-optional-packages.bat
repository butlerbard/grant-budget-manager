@echo off
cd /d "%~dp0"
title 安裝或更新選用功能

if not exist ".venv\Scripts\python.exe" (
    echo 尚未建立本系統專用環境 .venv。
    echo 請先執行「啟動經費管理.bat」完成初次設定，再回來安裝套件。
    goto finish
)

".venv\Scripts\python.exe" src\manage_optional_packages.py menu

:finish
echo.
echo 按任意鍵關閉視窗。
pause >nul
