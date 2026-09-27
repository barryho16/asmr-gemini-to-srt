# -*- coding: utf-8 -*-

import sys
import shutil
from pathlib import Path


# ============================================================
# 程式結束時是否需要按 Enter 才退出
# True = 需要按 Enter
# False = 直接退出
WAIT_FOR_ENTER_BEFORE_EXIT = False
# ============================================================


def wait_before_exit():
    """根據設定決定是否等待使用者按 Enter。"""
    if WAIT_FOR_ENTER_BEFORE_EXIT:
        input("按 Enter 退出...")


def main():
    try:
        # 獲取當前 .py 檔案所在資料夾
        script_dir = Path(__file__).resolve().parent

        # 源檔案：當前資料夾下的 References.txt
        source_file = script_dir / "References.txt"

        # 目標資料夾：當前資料夾\merged_srt\translated_srt
        target_dir = script_dir / "merged_srt"

        # 檢查 References.txt 是否存在
        if not source_file.exists():
            raise FileNotFoundError(
                f"找不到文件：{source_file}\n"
                f"请确认 References.txt 和这个 .py 文件在同一个文件夹里。"
            )

        # 如果目標資料夾不存在，則自動建立
        target_dir.mkdir(parents=True, exist_ok=True)

        # 目標檔案路徑
        target_file = target_dir / source_file.name

        # 複製檔案，保留元資料；如果目標已存在，會覆蓋
        shutil.copy2(source_file, target_file)

        print("复制成功！")
        print(f"源文件：{source_file}")
        print(f"目标文件：{target_file}")

    except Exception as e:
        print("程序执行出错：")
        print(e)

    finally:
        wait_before_exit()


if __name__ == "__main__":
    main()