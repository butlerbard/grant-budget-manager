from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parent
ROOT = SRC_DIR.parent
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def main():
    if sys.version_info < (3, 8):
        print("本系統需要 Python 3.8 或更新版本。")
        answer = input("要使用 winget 安裝 Python 3.14 嗎？[Y/N] ").strip().lower()
        if answer in ("y", "yes", "是"):
            try:
                result = subprocess.run([
                    "winget", "install", "--id", "Python.Python.3.14", "--exact", "--source", "winget"
                ])
            except FileNotFoundError:
                print("找不到 winget，請從 Python 官方網站安裝新版 Python。")
                return 1
            if result.returncode == 0:
                print("Python 安裝完成。請關閉此視窗，再重新執行啟動檔。")
        return 1

    if not VENV_PYTHON.exists():
        print("首次設定：正在建立本系統專用的 Python 環境（.venv）...")
        result = subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], cwd=ROOT)
        if result.returncode or not VENV_PYTHON.exists():
            print("建立 .venv 失敗；尚未安裝任何套件，也未啟動系統。")
            return 1

    check = subprocess.run([str(VENV_PYTHON), "--version"], cwd=ROOT)
    if check.returncode:
        print("無法使用 .venv 中的 Python。請確認本機 Python 安裝仍存在。")
        return 1

    subprocess.run([str(VENV_PYTHON), str(SRC_DIR / "manage_optional_packages.py"), "startup"], cwd=ROOT)
    print("正在啟動經費管理系統...")
    return subprocess.run([str(VENV_PYTHON), str(SRC_DIR / "app.py")], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
