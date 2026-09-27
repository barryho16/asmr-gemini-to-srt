#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
字幕時間軸強制鎖定與嚴格校驗工具 (純校驗版 - 已移除 AI 翻譯功能)
🔒 功能：讀取「原文 SRT」與「translate_srt 資料夾內對應的翻譯後 SRT」
         進行嚴格校驗（條目數 / 序號一致性），通過後強制鎖定原文時間軸
         並重組輸出最終 SRT。任一環節失敗會直接明確告知原因。

使用方式：
1. 將原文 .srt 檔案放在本程式所在的資料夾。
2. 使用網頁版 AI（ChatGPT / Gemini / Claude 等）手動翻譯後，
   將翻譯結果存成「同檔名」的 .srt，放入 translate_srt 資料夾。
3. 執行本程式，結果會輸出到 final_srt 資料夾。
"""

import os
import re
from typing import List, Dict, Tuple

# ======================================================
# 🔧【設定區】
# ======================================================

# 存放「已用網頁版 AI 翻譯完成」的 SRT 檔案的資料夾名稱（輸入）
TRANSLATE_FOLDER_NAME = "translated_srt"

# 校驗與時間軸鎖定完成後，最終輸出的資料夾名稱
OUTPUT_FOLDER_NAME = "final_srt"

# 程式結束時是否需要按 Enter 才退出
# True = 需要按 Enter
# False = 直接退出
WAIT_FOR_ENTER_BEFORE_EXIT = True

# 結束時的提示文字
EXIT_PROMPT_TEXT = "\n所有處理完成，按 Enter 键结束程序..."


# ======================================================
# 🔧【通用輔助函式】
# ======================================================

def wait_for_exit_if_needed(prompt_text: str = None):
    """
    根據設定決定是否在程式退出前等待使用者按 Enter。
    同時相容某些無互動環境，避免 input() 拋出 EOFError。
    """
    if not WAIT_FOR_ENTER_BEFORE_EXIT:
        return

    final_prompt = prompt_text if prompt_text is not None else EXIT_PROMPT_TEXT
    try:
        input(final_prompt)
    except EOFError:
        pass


def clean_markdown_wrapper(text: str) -> str:
    """
    清理翻譯檔內容中可能殘留的 Markdown 程式碼區塊標記
    （例如網頁版 AI 回覆時常見的 ```srt ... ``` 包裝）
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```[a-z]*\n?', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\n?```$', '', text, flags=re.IGNORECASE)
    return text.strip()


# ======================================================
# 🔒【核心解析與校驗函式】
# ======================================================

def parse_srt_strict(srt_content: str) -> List[Dict]:
    """
    🔒 嚴格解析 SRT 格式

    判定規則：當且僅當某一行為純數字，且其下一行嚴格匹配標準 SRT 時間軸格式
    （如 00:00:00,000 --> 00:00:00,000）時，才視為序號。
    否則一律視為字幕文本內容，避免純數字文本干擾。

    傳回格式：[{
        'index': int,           # 序號
        'timestamp_line': str,  # 原始時間軸字串（完整保留，用於後續鎖定）
        'text': str             # 字幕文本內容
    }, ...]
    """
    lines = srt_content.split('\n')
    entries = []
    i = 0

    # 標準 SRT 時間軸正則表達式（嚴格匹配 HH:MM:SS,mmm --> HH:MM:SS,mmm 格式）
    timestamp_pattern = re.compile(
        r'^\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*$'
    )

    while i < len(lines):
        # 跳過空行
        if not lines[i].strip():
            i += 1
            continue

        # 檢查是否為純數字（候選序號）
        if not re.match(r'^\s*\d+\s*$', lines[i]):
            i += 1
            continue

        # 檢查下一行是否為標準時間軸格式
        if i + 1 >= len(lines):
            i += 1
            continue

        next_line = lines[i + 1]
        if not timestamp_pattern.match(next_line):
            # 🔒 下一行不是時間軸格式，這個純數字不是序號，跳過
            i += 1
            continue

        # 🔒 確認為有效序號
        index = int(lines[i].strip())
        timestamp_line = next_line.strip()

        # 收集文本內容（直到下一個有效序號或檔案結束）
        i += 2  # 跳過序號行和時間軸行
        text_lines = []

        while i < len(lines):
            # 檢查是否遇到下一個有效條目的序號
            if (re.match(r'^\s*\d+\s*$', lines[i]) and
                i + 1 < len(lines) and
                timestamp_pattern.match(lines[i + 1])):
                break

            # 如果是空行，判斷是否為條目分隔符
            if not lines[i].strip():
                # 如果下下行是序號+時間軸組合，則當前空行為分隔符
                if (i + 1 < len(lines) and
                    re.match(r'^\s*\d+\s*$', lines[i + 1]) and
                    i + 2 < len(lines) and
                    timestamp_pattern.match(lines[i + 2])):
                    i += 1  # 跳過分隔空行
                    break
                # 否則保留為文本中的空行
                text_lines.append('')
                i += 1
                continue

            # 普通文本行
            text_lines.append(lines[i])
            i += 1

        # 組裝條目
        text = '\n'.join(text_lines).strip()
        entries.append({
            'index': index,
            'timestamp_line': timestamp_line,
            'text': text
        })

    return entries


def validate_translation(original_entries: List[Dict], translated_entries: List[Dict]) -> Tuple[bool, str]:
    """
    🔒 嚴格校驗「翻譯後 SRT」與「原始 SRT」是否對應

    校驗規則：
    1. 總數必須完全一致（不允許條目增減）
    2. 序號必須逐條一一對應（確保順序正確）

    傳回：(是否通過校驗, 錯誤資訊)
    """
    # 🔒 校驗 1：總數必須完全一致
    if len(original_entries) != len(translated_entries):
        error_msg = (f"條目總數不一致！原文: {len(original_entries)} 條, "
                     f"翻譯後: {len(translated_entries)} 條")
        return False, error_msg

    # 🔒 校驗 2：逐條比對序號，確保順序與數量一一對應
    for i, (orig, trans) in enumerate(zip(original_entries, translated_entries)):
        if orig['index'] != trans['index']:
            error_msg = (f"序號不一致！位置 {i+1}: 原文序號 {orig['index']}, "
                         f"翻譯後序號 {trans['index']}")
            return False, error_msg

    return True, ""


def lock_and_rebuild_srt(original_entries: List[Dict], translated_entries: List[Dict]) -> str:
    """
    🔒 時間軸強制鎖定與重組（核心機制）

    策略：完全丟棄翻譯後 SRT 中的時間軸（不論是否被網頁版 AI 竄改），
    直接從原始 SRT 中按順序擷取對應的原始時間軸字串，
    與翻譯後的純文本重新拼裝成標準 SRT 格式。

    傳回：重組後的標準 SRT 字符串
    """
    result_lines = []

    for orig, trans in zip(original_entries, translated_entries):
        # 使用原始序號
        result_lines.append(str(orig['index']))

        # 🔒 強制使用原始時間軸（完全鎖定，不受翻譯檔內容影響）
        result_lines.append(orig['timestamp_line'])

        # 使用翻譯後的文本內容
        result_lines.append(trans['text'])

        # 條目分隔空行
        result_lines.append('')

    return '\n'.join(result_lines)


# ======================================================
# 核心檔案處理函式
# ======================================================

def process_single_file(original_path: str, translated_path: str, output_path: str) -> Tuple[bool, str]:
    """
    處理單一檔案的完整流程：
    1. 讀取原文與翻譯後檔案
    2. 嚴格解析兩者
    3. 嚴格校驗（總數 + 序號）
    4. 時間軸強制鎖定與重組
    5. 寫入輸出檔案

    傳回：(是否成功, 失敗原因訊息（成功時為空字串）)
    """
    # 讀取原文檔案
    try:
        with open(original_path, "r", encoding="utf-8") as f:
            original_content = f.read()
    except Exception as e:
        return False, f"讀取原文檔案失敗: {e}"

    # 讀取翻譯後檔案
    try:
        with open(translated_path, "r", encoding="utf-8") as f:
            translated_content = f.read()
    except Exception as e:
        return False, f"讀取翻譯後檔案失敗: {e}"

    # 清理翻譯後內容可能殘留的 Markdown 包裝（例如複製自網頁版 AI 回覆時帶入的 ```）
    translated_content = clean_markdown_wrapper(translated_content)

    # 🔒 步驟 1: 嚴格解析原文 SRT
    original_entries = parse_srt_strict(original_content)
    if not original_entries:
        return False, "原文 SRT 未能解析出任何有效字幕條目（請檢查格式）"

    # 🔒 步驟 2: 嚴格解析翻譯後 SRT
    translated_entries = parse_srt_strict(translated_content)
    if not translated_entries:
        return False, "翻譯後 SRT 未能解析出任何有效字幕條目（請檢查格式）"

    # 🔒 步驟 3: 嚴格校驗（總數 + 序號）
    is_valid, error_msg = validate_translation(original_entries, translated_entries)
    if not is_valid:
        return False, f"校驗失敗: {error_msg}"

    # 🔒 步驟 4: 時間軸強制鎖定與重組
    final_srt = lock_and_rebuild_srt(original_entries, translated_entries)

    # 🔒 步驟 5: 寫入輸出檔案
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_srt)
    except Exception as e:
        return False, f"寫入輸出檔案失敗: {e}"

    return True, ""


def main():
    folder = os.path.dirname(os.path.abspath(__file__))
    translate_folder = os.path.join(folder, TRANSLATE_FOLDER_NAME)
    output_folder = os.path.join(folder, OUTPUT_FOLDER_NAME)

    print("🔒 字幕時間軸強制鎖定與嚴格校驗工具")
    print("📌 已完全移除 AI 翻譯功能，請自行使用網頁版 AI 完成翻譯後放入指定資料夾")
    print("=" * 60)

    # 檢查 translate_srt 資料夾是否存在
    if not os.path.exists(translate_folder):
        print(f"❌ 找不到 {TRANSLATE_FOLDER_NAME} 資料夾！")
        print(f"   請在本程式旁建立 {TRANSLATE_FOLDER_NAME} 資料夾，")
        print(f"   並將翻譯後的 SRT（檔名需與原文相同）放入其中。")
        wait_for_exit_if_needed("\n按 Enter 鍵退出...")
        return

    # 建立輸出資料夾
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
        print(f"📁 已創建輸出文件夾: {OUTPUT_FOLDER_NAME}")

    # 找出本資料夾中的原文 SRT（排除 translate_srt / final_srt 內的檔案，因為 os.listdir 只列出當前層級，本來就不會誤抓）
    original_files = [f for f in os.listdir(folder) if f.lower().endswith('.srt')]

    if not original_files:
        print("❌ 在本資料夾中找不到任何原文 SRT 文件")
        wait_for_exit_if_needed("\n按 Enter 鍵退出...")
        return

    print(f"📝 找到 {len(original_files)} 個原文 SRT 文件，開始逐一校驗與處理...")
    print("=" * 60)

    success_count = 0
    failed_count = 0
    failed_files = []  # [(檔名, 原因), ...]

    for idx, filename in enumerate(original_files, 1):
        original_path = os.path.join(folder, filename)
        translated_path = os.path.join(translate_folder, filename)
        output_path = os.path.join(output_folder, filename)

        print(f"\n[{idx}/{len(original_files)}] 📄 處理中: {filename}")

        # 檢查對應的翻譯檔是否存在
        if not os.path.exists(translated_path):
            reason = f"在 {TRANSLATE_FOLDER_NAME} 資料夾中找不到對應的翻譯後檔案"
            print(f"    ❌ 失敗: {reason}")
            failed_count += 1
            failed_files.append((filename, reason))
            continue

        success, reason = process_single_file(original_path, translated_path, output_path)

        if success:
            print(f"    ✅ 校驗通過，時間軸已鎖定，成功輸出: {filename}")
            success_count += 1
        else:
            print(f"    ❌ 失敗: {reason}")
            failed_count += 1
            failed_files.append((filename, reason))

    print("\n" + "=" * 60)
    print("🎉 全部處理完成！")
    print(f"   ✅ 成功: {success_count}")
    print(f"   ❌ 失敗: {failed_count}")
    print(f"   📁 輸出位置: {output_folder}")

    if failed_files:
        print("\n⚠️ 以下檔案處理失敗，請檢查並修正後重新執行：")
        for fname, reason in failed_files:
            print(f"   - {fname}\n       原因: {reason}")

    wait_for_exit_if_needed(EXIT_PROMPT_TEXT)


if __name__ == "__main__":
    main()