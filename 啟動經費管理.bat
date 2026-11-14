@echo off
cd /d "%~dp0"
title 經費管理系統

python --version >nul 2>&1
if errorlevel 1 goto no_python

python src\start_budget.py
if errorlevel 1 echo 啟動程序結束，錯誤代碼：%errorlevel%
goto finish

:no_python
echo 找不到可用的 Python，主系統無法啟動。
choice /c YN /n /m "要使用 winget 安裝 Python 3.14 嗎？ [Y/N] "
if errorlevel 2 goto finish
where winget >nul 2>&1
if errorlevel 1 (
    echo 找不到 winget。請從 Python 官方網站安裝 Python：
    echo https://www.python.org/downloads/windows/
    goto finish
)
winget install --id Python.Python.3.14 --exact --source winget
if errorlevel 1 (
    echo Python 安裝未完成。請查看上方錯誤訊息。
    goto finish
)
echo Python 安裝完成。請關閉此視窗，再重新執行啟動檔。

:finish
echo.
echo 按任意鍵關閉視窗。
pause >nul
