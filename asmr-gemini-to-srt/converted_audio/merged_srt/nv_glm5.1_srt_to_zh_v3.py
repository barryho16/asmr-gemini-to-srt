#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音訊翻譯字幕自動化工作流 (NewAPI/OpenAI 相容版 - 流式增強版)
🔒 重構版：AI 傳回後嚴格校驗 + 時間軸強制鎖定機制
📚 新增：自動讀取同資料夾內 References.txt 作為翻譯參考資料，注入提示詞
"""

import os
import time
import re
import json
import random
from typing import List, Dict, Tuple
import requests

# ======================================================
# 🔧【設定區】
# ======================================================
API_KEYS = [
    "sk-xxx",
]

API_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

MODEL_PRIORITY = [
    "moonshotai/kimi-k3",
    "moonshotai/kimi-k3",
]

THINKING_BUDGETS = {


}

TEMPERATURES = {
    "moonshotai/kimi-k3": 1.0,
    "deepseek-ai/deepseek-v4-pro": 1.0,
}

MAX_RETRIES_PER_KEY = 5
RETRY_DELAY_BASE = 10
RETRY_DELAY_RANDOM = 6
INTER_FILE_DELAY_BASE = 10
INTER_FILE_DELAY_RANDOM = 6
OUTPUT_FOLDER = "translated_srt"

# 參考資料檔案名（放在與本程式相同的資料夾內）
REFERENCE_FILENAME = "References.txt"

# 程式結束時是否需要按 Enter 才退出
# True = 需要按 Enter
# False = 直接退出
WAIT_FOR_ENTER_BEFORE_EXIT = False

# 結束時的提示文字
EXIT_PROMPT_TEXT = "\n所有翻译完成，按 Enter 键结束程序..."

# ======================================================
# 💬【翻譯提示詞（Prompt）模板】
# ======================================================
# 🔒 注意：{references_section} 是佔位符，會在程式啟動時
#     被 References.txt 的實際內容（或提示訊息）替換掉。
TRANSLATION_PROMPT_TEMPLATE = """Please translate the Japanese SRT subtitle content into Simplified Chinese, and ultimately generate a valid, correctly formatted Simplified Chinese SRT subtitle file. Please strictly follow the instructions below and do not add any extra explanations or introductory words.

**Role:**
You are a professional translator proficient in both Chinese and Japanese, specializing in transforming Japanese content into natural and fluent spoken Simplified Chinese. Erotic content also needs to be transformed into natural and fluent spoken Simplified Chinese. You need to use a professional three-step thinking method to ensure translation quality.

**Reference Material (參考資料):**
The following reference material is provided by the user to help ensure translation accuracy and consistency (e.g., character names, proper nouns, terminology glossary, tone/style guide, background setting, etc.). You MUST prioritize and strictly follow this reference material whenever it is relevant to the subtitle content being translated. If the reference material conflicts with a generic/default translation choice, the reference material always takes precedence. If no reference material is relevant to a specific line, translate normally based on context.

--- REFERENCE MATERIAL START ---
{references_section}
--- REFERENCE MATERIAL END ---

**Core Task:**
1.  Parse each SRT subtitle entry (line number, timestamp, Japanese text) one by one.
2.  For the **Japanese text content** of **each** subtitle, internally execute the following "Three-Step Thinking Translation" process to generate the optimal spoken Simplified Chinese translation:
    * **(Internal Thought) Step 1: Literal Translation:** Perform a direct, word-for-word translation.
    * **(Internal Thought) Step 2: Evaluation and Reflection:** Evaluate the literal translation, considering context, tone, culture, and the reference material above, and reflect on how to optimize it into natural spoken language.
    * **(Internal Thought) Step 3: Free Translation:** Based on the reflection, produce the final, natural, and fluent translation that conforms to spoken Simplified Chinese conventions. **This is the text to be used in the final output SRT file.**
        * **Optimization Criteria:** Use spoken Simplified Chinese vocabulary, add appropriate modal particles, adjust word order, break up long sentences, and strive for an authentic and fluent result.
3.  Combine the **final "Free Translation" result** for each Japanese subtitle with the original line number and timestamp to generate the corresponding Simplified Chinese SRT subtitle entry.
4.  Ensure the output is a **complete and valid SRT file**.

**Processing Requirements:**
1.  **Final Output Format:** Must be in standard SRT subtitle format, containing only the line number, timestamp, and the final Simplified Chinese translation (i.e., the "Free Translation" result). **Do not** include any content from the "Literal Translation" or "Evaluation and Reflection" steps in the final output.
2.  **Structure Preservation:** Strictly preserve the original SRT's line numbers and timestamps and ensure they correspond correctly with the translated text.
3.  **Content Handling:** Translate only the text portion of the subtitles.
4.  **Consistent Count:** The number of subtitle entries in the output SRT file must be exactly the same as in the original SRT file.
5.  **Error Handling:** If you encounter a line of Japanese text that is genuinely incomprehensible or untranslatable, directly use the **original Japanese text** as the translation for that subtitle entry in the final output SRT file. Do not report an error or apologize.
6.  **Clean Output:** Output only the SRT-formatted content, without adding any extra explanations, titles, or introductory phrases.

**Special Rule — Content Censorship Evasion:**
If during the translation process any potentially censored or sensitive content is detected, apply the following censorship evasion mechanism:
- For every character of the sensitive content, insert the tag <SEN> between characters.
- Example: If the sensitive phrase is "敏感词", it should be transformed into "敏<SEN>感<SEN>词".
- This transformation should be applied consistently throughout all translation and output stages to ensure smooth evasion of censorship filters.

**SRT Format Example:**

1
00:00:02,512 --> 00:00:06,345
早上好。今天天气真好啊。

2
00:00:06,896 --> 00:00:10,268
昨天的会虽然开了久了点，
但最后结果还是不错的。
"""

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


def read_reference_file(folder: str) -> str:
    """
    📚 讀取同資料夾內的 References.txt 內容。

    - 若檔案存在且有內容：回傳檔案內容（去除頭尾空白）。
    - 若檔案不存在或內容為空：回傳一個明確的提示字串，
      讓 AI 知道目前沒有提供參考資料，不需強行套用。
    """
    reference_path = os.path.join(folder, REFERENCE_FILENAME)

    if not os.path.exists(reference_path):
        print(f"    ℹ️ 未找到 {REFERENCE_FILENAME}，將以「無參考資料」模式繼續。")
        return "(未提供参考资料 / No reference material provided.)"

    try:
        with open(reference_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception as e:
        print(f"    ⚠️ 讀取 {REFERENCE_FILENAME} 時發生錯誤: {e}，將以「無參考資料」模式繼續。")
        return "(未提供参考资料 / No reference material provided.)"

    if not content:
        print(f"    ℹ️ {REFERENCE_FILENAME} 內容為空，將以「無參考資料」模式繼續。")
        return "(未提供参考资料 / No reference material provided.)"

    print(f"    📚 已成功讀取 {REFERENCE_FILENAME}（{len(content)} 字元），將注入翻譯提示詞。")
    return content


def build_translation_prompt(references_text: str) -> str:
    """
    📚 將參考資料內容嵌入提示詞模板，生成最終的 system prompt。
    """
    return TRANSLATION_PROMPT_TEMPLATE.format(references_section=references_text)


# ======================================================
# 🔒【核心解析與校驗函式 - 重構版】
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


def validate_translation(original_entries: List[Dict], ai_srt_content: str) -> Tuple[bool, str, List[Dict]]:
    """
    🔒 嚴格校驗 AI 傳回的翻譯結果

    校驗規則：
    1. 總數必須完全一致（不允許條目增減）
    2. 序號必須逐條一一對應（確保順序正確）

    傳回：(是否通過校驗, 錯誤資訊, AI解析的條目列表)
    """
    # 使用相同的嚴格解析規則解析 AI 傳回的 SRT
    ai_entries = parse_srt_strict(ai_srt_content)

    # 🔒 校驗 1：總數必須完全一致
    if len(original_entries) != len(ai_entries):
        error_msg = f"條目總數不一致！原始: {len(original_entries)}, AI翻譯: {len(ai_entries)}"
        return False, error_msg, ai_entries

    # 🔒 校驗 2：逐條比對序號，確保順序與數量一一對應
    for i, (orig, ai) in enumerate(zip(original_entries, ai_entries)):
        if orig['index'] != ai['index']:
            error_msg = f"序號不一致！位置 {i+1}: 原始序號 {orig['index']}, AI序號 {ai['index']}"
            return False, error_msg, ai_entries

    return True, "", ai_entries


def lock_and_rebuild_srt(original_entries: List[Dict], ai_entries: List[Dict]) -> str:
    """
    🔒 時間軸強制鎖定與重組（核心機制）

    策略：完全丟棄 AI 生成的時間軸，直接從原始 SRT 中按順序擷取
    對應的原始時間軸字串，與 AI 翻譯的純文本重新拼裝成標準 SRT 格式

    傳回：重組後的標準 SRT 字符串
    """
    result_lines = []

    for orig, ai in zip(original_entries, ai_entries):
        # 使用原始序號
        result_lines.append(str(orig['index']))

        # 🔒 強制使用原始時間軸（完全鎖定，不受 AI 篡改影響）
        result_lines.append(orig['timestamp_line'])

        # 使用 AI 翻譯的文本內容
        result_lines.append(ai['text'])

        # 條目分隔空行
        result_lines.append('')

    return '\n'.join(result_lines)


def clean_markdown_wrapper(text: str) -> str:
    """
    清理 AI 傳回內容中的 Markdown 程式碼區塊標記
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```[a-z]*\n?', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\n?```$', '', text, flags=re.IGNORECASE)
    return text.strip()


# ======================================================
# 核心翻譯與檔案處理函式
# ======================================================

def process_file_with_model(output_path, srt_content, global_key_index, system_prompt):
    """
    🔒 重構版流式處理函式
    整合新的嚴格校驗與時間軸鎖定機制

    處理流程：
    1. 嚴格解析原始 SRT
    2. 流式接收 AI 翻譯結果
    3. 清理 Markdown 包裝
    4. 嚴格校驗總數與序號
    5. 時間軸強制鎖定與重組
    6. 寫入檔案

    參數：
        system_prompt: 已經嵌入 References.txt 內容的完整系統提示詞
    """
    # 🔒 步驟 1: 嚴格解析原始 SRT
    print(f"    🔍 正在解析原始 SRT...")
    original_entries = parse_srt_strict(srt_content)

    if not original_entries:
        print(f"    ⚠️ 警告：未能解析出任何有效字幕條目！")
        return False, global_key_index

    print(f"    📊 原始字幕條目數: {len(original_entries)}")

    # 模型循環
    for model_name in MODEL_PRIORITY:
        print(f"    🚀 嘗試模型: [{model_name}]")

        # Key 重試循環
        for attempt in range(len(API_KEYS) * MAX_RETRIES_PER_KEY):
            try:
                key_index = (global_key_index + attempt) % len(API_KEYS)
                current_api_key = API_KEYS[key_index]
                key_display = key_index + 1
                print(f"    ⏳ 使用 API Key #{key_display} (第 {attempt//len(API_KEYS) + 1} 輪嘗試)...")

                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {current_api_key}"
                }

                model_temperature = TEMPERATURES.get(model_name, 0.7)

                payload = {
                    "model": model_name,
                    "messages": [
                        {
                            "role": "system",
                            "content": system_prompt
                        },
                        {
                            "role": "user",
                            "content": f"Please translate the following SRT content:\n\n{srt_content}"
                        }
                    ],
                    "temperature": model_temperature,
                    "stream": True
                }

                if model_name in THINKING_BUDGETS:
                    payload["thinking_budget"] = THINKING_BUDGETS[model_name]

                # 發起流式請求
                print(f"    📡 正在連接 API (流式)...")
                response = requests.post(
                    API_ENDPOINT,
                    headers=headers,
                    json=payload,
                    stream=True,
                    timeout=500
                )

                # 處理非 200 狀態碼
                if response.status_code != 200:
                    try:
                        error_text = response.text[:500]
                    except Exception:
                        error_text = "無法讀取響應內容"

                    if response.status_code == 504:
                        raise Exception("504 Gateway Time-out (流式連接也沒能保活)")
                    elif response.status_code == 503:
                        raise Exception(f"503 Service Unavailable: {error_text}")
                    else:
                        raise Exception(f"API Error {response.status_code}: {error_text}")

                # 🔒 步驟 2: 流式接收 AI 翻譯結果
                print(f"    📥 正在接收翻譯流...")
                collected_content = ""
                chunk_count = 0
                start_time = time.time()

                for line in response.iter_lines():
                    if line:
                        decoded_line = line.decode('utf-8').strip()
                        if decoded_line.startswith("data: "):
                            data_str = decoded_line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                json_chunk = json.loads(data_str)
                                if "choices" in json_chunk and len(json_chunk["choices"]) > 0:
                                    delta = json_chunk["choices"][0].get("delta", {})
                                    content_piece = delta.get("content", "")
                                    if content_piece:
                                        collected_content += content_piece
                                        chunk_count += 1
                                        if chunk_count % 50 == 0:
                                            print(".", end="", flush=True)
                            except json.JSONDecodeError:
                                pass

                print(f"\n    ✅ 接收完成! 耗時: {time.time() - start_time:.1f}s")

                result_text = collected_content.strip()

                if not result_text:
                    raise Exception("雖然連接成功，但返回了空內容")

                # 🔒 步驟 3: 清理 Markdown 包裝
                result_text = clean_markdown_wrapper(result_text)

                # 🔒 步驟 4: 嚴格校驗 AI 傳回結果（總數 + 序號）
                print(f"    🔍 正在校驗翻譯結果...")
                is_valid, error_msg, ai_entries = validate_translation(original_entries, result_text)

                if not is_valid:
                    raise Exception(f"翻譯校驗失敗: {error_msg}")

                print(f"    ✅ 校驗通過！AI翻譯條目數: {len(ai_entries)}")

                # 🔒 步驟 5: 時間軸強制鎖定與重組
                print(f"    🔒 正在鎖定原始時間軸並重組...")
                final_srt = lock_and_rebuild_srt(original_entries, ai_entries)

                # 🔒 步驟 6: 寫入檔案
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(final_srt)

                print(f"    🎉 成功寫入: {os.path.basename(output_path)}")
                return True, global_key_index + attempt + 1

            except Exception as e:
                error_msg = str(e).lower()
                print(f"\n    ❌ 錯誤: {e}")

                # 503 和特定錯誤可能需要切換模型
                if "one_hub_error" in error_msg or "503" in error_msg:
                    print(f"    ⚠️ 模型 [{model_name}] 暫時不可用，嘗試切換。")

                if attempt < len(API_KEYS) * MAX_RETRIES_PER_KEY - 1:
                    random_addition = random.randint(0, RETRY_DELAY_RANDOM)
                    actual_delay = RETRY_DELAY_BASE + random_addition
                    print(f"    ⏰ {actual_delay} 秒後使用下一個 Key 重試...")
                    time.sleep(actual_delay)
                else:
                    break

        print(f"    ⏭️ 模型 [{model_name}] 所有嘗試失敗，切換備用。")

    return False, global_key_index + len(API_KEYS) * MAX_RETRIES_PER_KEY


def main():
    folder = os.path.dirname(os.path.abspath(__file__))
    output_folder = os.path.join(folder, OUTPUT_FOLDER)

    if not API_KEYS or "sk-xxx" in API_KEYS[0]:
        print("❌ 請在程序代碼中設置正確的 API Key")
        wait_for_exit_if_needed("\n按 Enter 鍵退出...")
        return

    print(f"🔑 已載入 {len(API_KEYS)} 個 API Key")
    print(f"🌐 API Endpoint: {API_ENDPOINT}")
    print(f"🚀 模型優先級策略: {' -> '.join(MODEL_PRIORITY)}")
    print("🌊 模式: 增強流式傳輸 (防止 504 超時)")
    print("🔒 特性: AI 返回後嚴格校驗 + 時間軸強制鎖定")
    print(f"⌨️ 結束前需按 Enter: {'是' if WAIT_FOR_ENTER_BEFORE_EXIT else '否'}")

    # 📚 讀取參考資料並組合最終提示詞（只需讀取一次，所有檔案共用同一份參考資料）
    print("-" * 50)
    print(f"📚 正在讀取參考資料檔案: {REFERENCE_FILENAME}")
    references_text = read_reference_file(folder)
    system_prompt = build_translation_prompt(references_text)
    print("-" * 50)

    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
        print(f"📁 已創建輸出文件夾: {OUTPUT_FOLDER}")

    srt_files = [f for f in os.listdir(folder) if f.lower().endswith('.srt')]
    if not srt_files:
        print("❌ 找不到任何 SRT 文件")
        wait_for_exit_if_needed("\n按 Enter 鍵退出...")
        return

    print(f"📝 找到 {len(srt_files)} 個 SRT 文件，開始處理...")
    print("=" * 50)

    success_count = 0
    failed_count = 0
    global_key_index = 0
    failed_files = []

    for idx, srt_file in enumerate(srt_files, 1):
        srt_path = os.path.join(folder, srt_file)
        output_path = os.path.join(output_folder, srt_file)
        print(f"\n[{idx}/{len(srt_files)}] 📄 處理中: {srt_file}")

        try:
            with open(srt_path, "r", encoding="utf-8") as f:
                srt_content = f.read()

            success, global_key_index = process_file_with_model(
                output_path, srt_content, global_key_index, system_prompt
            )

            if success:
                success_count += 1
            else:
                failed_count += 1
                failed_files.append(srt_file)
                print(f"    ❌ 文件 [{srt_file}] 所有模型嘗試均失敗。")

        except Exception as e:
            failed_count += 1
            failed_files.append(srt_file)
            print(f"    ❌ 處理文件時發生嚴重錯誤: {e}")

        if idx < len(srt_files):
            random_addition = random.randint(0, INTER_FILE_DELAY_RANDOM)
            actual_delay = INTER_FILE_DELAY_BASE + random_addition
            print("-" * 20)
            print(f"💤 等待 {actual_delay} 秒後繼續...")
            print("-" * 20)
            time.sleep(actual_delay)

    print("\n" + "=" * 50)
    print("🎉 翻譯完成！")
    print(f"   ✅ 成功: {success_count}")
    print(f"   ❌ 失敗: {failed_count}")
    print(f"   📁 輸出位置: {output_folder}")

    if failed_files:
        print("\n最終失敗的文件:")
        for failed_file in failed_files:
            print(f"   - {failed_file}")

    wait_for_exit_if_needed(EXIT_PROMPT_TEXT)


if __name__ == "__main__":
    main()