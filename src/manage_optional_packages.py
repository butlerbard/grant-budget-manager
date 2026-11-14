from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.specifiers import SpecifierSet


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "經費管理系統"
REQUIREMENTS = {}


def load_requirements():
    for raw_line in (Path(__file__).resolve().parent / "requirements.txt").read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            requirement = Requirement(line)
            REQUIREMENTS[requirement.name.lower()] = line


def is_satisfied(name):
    requirement = Requirement(REQUIREMENTS[name])
    try:
        installed = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return installed in requirement.specifier


def install(names):
    args = [sys.executable, "-m", "pip", "install", "--upgrade"]
    args.extend(REQUIREMENTS[name] for name in names)
    result = subprocess.run(args, cwd=PROJECT_ROOT)
    if result.returncode:
        print("安裝或更新未完成；主系統仍可使用。")
        return False
    if not all(is_satisfied(name) for name in names):
        print("安裝後版本檢查未通過。")
        return False
    for name in names:
        marker = STATE_DIR / ("略過_PDF套件.txt" if name in ("reportlab", "pypdf") else f"略過_{name}.txt")
        marker.unlink(missing_ok=True)
    print("安裝完成，版本檢查通過。")
    return True


def skipped(marker_name):
    return (STATE_DIR / marker_name).exists()


def remember_skip(marker_name):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / marker_name).write_text("略過此項目\n", encoding="utf-8")


def ask_install(prompt):
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("y", "yes", "是"):
            return True
        if answer in ("n", "no", "否"):
            return False


def startup_check():
    pdf_names = ("reportlab", "pypdf")
    pdf_marker = "略過_PDF套件.txt"
    if all(is_satisfied(name) for name in pdf_names):
        print("[符合需求] PDF 功能套件")
    elif skipped(pdf_marker):
        print("[已略過] PDF 功能套件")
    else:
        print("PDF 功能需要安裝 ReportLab 和 pypdf。")
        if ask_install("是否現在安裝或更新？ [Y/N] "):
            install(pdf_names)
        else:
            remember_skip(pdf_marker)
            print("已記住選擇；PDF 功能暫不可用。")

    keyring_marker = "略過_keyring.txt"
    if is_satisfied("keyring"):
        print("[符合需求] Email 寄送套件")
    elif skipped(keyring_marker):
        print("[已略過] Email 寄送套件")
    else:
        print("Email 寄送功能需要 keyring。")
        print("未安裝時仍可使用系統，但無法寄送 Email。")
        if ask_install("是否現在安裝或更新？ [Y/N] "):
            install(("keyring",))
        else:
            remember_skip(keyring_marker)
            print("已記住選擇；Email 寄送無法使用。")


def package_menu():
    print("請選擇要安裝或更新的功能：")
    print("1. PDF 匯出與 PDF 郵件附件")
    print("2. Email 寄送")
    print("3. 全部安裝或更新")
    print("4. 離開")
    choice = input("請輸入選項 [1-4]：").strip()
    if choice == "1":
        names = ("reportlab", "pypdf")
    elif choice == "2":
        names = ("keyring",)
    elif choice == "3":
        names = ("reportlab", "pypdf", "keyring")
    else:
        return

    if all(is_satisfied(name) for name in names):
        if not ask_install("所選套件已符合需求。仍要更新到目前可用版本嗎？ [Y/N] "):
            return
    install(names)


def main():
    load_requirements()
    if len(sys.argv) < 2:
        return 2
    if sys.argv[1] == "startup":
        startup_check()
    elif sys.argv[1] == "menu":
        package_menu()
    else:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
