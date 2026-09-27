# -*- coding: utf-8 -*-
# gemini-3.1-lite-to-srt.py — v3 整合版
# 內容：雙階段轉錄/校對 + 時間戳壞格式修復（04:39:458 → 00:04:39,458）
#       + Stage1/Stage2 時間軸交叉驗證（衝突時採用 Stage1 時間軸 + Stage2 文字）
#       + ★ v3：VPN/網路保活背景執行緒（AI 長時間思考期間定期產生小流量，
#               防止 VPN 因閒置被斷線 → [Errno -3] DNS 解析失敗）
# 檔案開頭標記：本行 ｜ 檔案結尾標記：最後一行 "main()"

import os
import re
import json
import time
import threading
import requests
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Optional

from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from google import genai
from google.genai import types

# ============================================================================
# 頂層設定區
# ============================================================================
API_KEYS = [
    "YOUR_API_KEY_HERE",
    "YOUR_API_KEY_HERE",
    "YOUR_API_KEY_HERE",
    "YOUR_API_KEY_HERE",
    "YOUR_API_KEY_HERE",
    "YOUR_API_KEY_HERE",
    "YOUR_API_KEY_HERE",
]

# ============================================================================
# Discord Webhook 設定
# ============================================================================
DISCORD_WEBHOOK = "YOUR_DISCORD_WEBHOOK_URL_HERE"

# ============================================================================
# 外接 HDD 保活設定
# ============================================================================
KEEPALIVE_INTERVAL_SEC = 150  # 2.5 分鐘 = 150 秒
KEEPALIVE_FILENAME = ".hdd_keepalive"

# ============================================================================
# ★ v3：VPN / 網路保活設定
# （AI 長時間思考時 generate_content 阻塞、連線上沒有封包，
#   VPN 可能判定閒置而斷線 → 定期用極小 HTTP 請求製造流量保住 VPN 活躍）
# ============================================================================
NETWORK_KEEPALIVE_INTERVAL_SEC = 60   # 每 60 秒產生一次小流量
NETWORK_KEEPALIVE_TIMEOUT_SEC = 10     # 單次保活請求逾時（秒），失敗靜默跳過
NETWORK_KEEPALIVE_URLS = [
    # Google 官方連通性檢查端點：回應 204、零內容，最省流量
    "https://www.gstatic.com/generate_204",
    "https://connectivitycheck.gstatic.com/generate_204",
    # 備用
    "https://cp.cloudflare.com/generate_204",
]
# 若你的 VPN 是「系統代理模式」而非 TUN/全域模式，且保活流量沒走 VPN，
# 可取消註解並改成你的代理位址：
# NETWORK_KEEPALIVE_PROXIES = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
NETWORK_KEEPALIVE_PROXIES = None

# ============================================================================
# 模型設定 —— 雙階段（支援多重備援）
# ============================================================================
TRANSCRIPTION_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

CORRECTION_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]

ENABLE_STAGE2 = True
ATTEMPTS_PER_MODEL = 3
STAGE1_MAX_RETRIES = len(TRANSCRIPTION_MODELS) * ATTEMPTS_PER_MODEL
STAGE2_MAX_RETRIES = len(CORRECTION_MODELS) * ATTEMPTS_PER_MODEL
MAX_TRANSIENT_RETRIES = 2

CHECK_SUBTITLE_LENGTH = True
MIN_COVERAGE_RATIO = 0.95
MAX_COVERAGE_RATIO = 1.0001

ACCEPT_CONSISTENT_COVERAGE = True
CONSISTENT_COVERAGE_COUNT = 4
CONSISTENT_COVERAGE_TOLERANCE = 0.01
ERROR_COVERAGE_RATIO = 1.05
ERROR_JSON_FILENAME = "error.json"

THINKING_LEVEL = "high"
THINKING_BUDGET = 24576
MODELS_WITHOUT_SAMPLING_PARAMS_PREFIXES = (
    "gemini-2.5",
    "gemini-3.7",
    "gemini-3.8",
    "gemini-4",
)
GEMINI_2X_PREFIXES = ("gemini-2.",)
TEMPERATURE = 1.0
TOP_P = 0.95
MEDIA_RESOLUTION_HIGH = True
RETRY_DELAY = 10
ENABLE_INTERNAL_RETRY = False

REFERENCE_FILENAME = "References.txt"
SAVE_INTERMEDIATE_JSON = True

SUPPORTED_AUDIO_FORMATS = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
}

# ============================================================================
# 時間軸修復 / Stage1-Stage2 交叉驗證設定（fix_srt_json v2 整合）
# ============================================================================
AUDIO_MAX_DURATION_FALLBACK_SEC = 605.0
TIMELINE_MATCH_TOL_SEC = 0.5
SAVE_MERGED_JSON = True
MAX_CONFLICT_PRINT = 20

SUBTITLE_JSON_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["index", "start", "end", "text"],
    },
}

# ============================================================================
# 提示詞：Stage 1 —— 轉錄
# ============================================================================
TRANSCRIPTION_BASE_PROMPT = (
    "你是一個專業的日語音訊轉字幕助手。你的任務是把輸入的日語音訊檔轉成高品質字幕。\n\n"
    "請嚴格遵守以下規則：\n\n"
    "先完成精確轉寫與時間對齊，再輸出字幕。不要先為了漂亮的句子而犧牲時間軸準確度。\n\n"
    "【核心目標】\n"
    "1. 時間軸正確\n"
    "2. 文字忠於原音\n"
    "3. 斷句自然\n"
    "4. 標點自然\n"
    "5. SRT 格式正確\n\n"
    "【轉寫原則】\n"
    "1. 準確轉寫日語語音內容，保留原本口語表達。\n"
    "2. 只修正明顯的 ASR 錯誤；如果音訊證據不足，不要過度改寫。\n"
    "3. 不要憑語意自行補充音訊中沒有說出的內容。\n\n"
    "【斷句原則】\n"
    "1. 以「語意完整 + 自然停頓 + 字幕可讀性」決定斷句。\n"
    "2. 優先在句尾、子句邊界、明顯停頓處切分。\n"
    "3. 不要把同一個短句切成過碎片段。\n"
    "4. 不要把兩個明顯獨立句子硬合成一條字幕。\n"
    "5. 若無明顯停頓，優先保持較長但語意完整的片段，而不是猜測細碎切點。\n\n"
    "【時間軸原則】\n"
    "1. 每條字幕的開始時間必須對應該段語音實際開始發聲的時間。\n"
    "2. 每條字幕的結束時間必須對應該段語音實際結束的時間。\n"
    "3. 不可讓字幕開始早於實際發聲。\n"
    "4. 不可讓字幕結束明顯晚於實際收音。\n"
    "5. 各條字幕時間必須單調遞增。\n"
    "6. 字幕之間不可重疊。\n"
    "7. 若時間點不確定，寧可減少切分數量，也不要捏造過細的時間點。\n"
    "8. 時間格式只能是 MM:SS,mmm（例：04:39,458），毫秒前面用「逗號」。\n"
    "   絕對禁止把毫秒用冒號接在後面（如 04:39:458），也絕對禁止使用 HH:MM:SS。\n\n"
    "【字幕內容原則】\n"
    "1. 每條字幕盡量只包含一個完整意思單位。\n"
    "2. 避免過長字幕。\n"
    "3. 避免只有助詞、語尾、接續詞單獨成行。\n"
    "4. 標點請使用自然日語標點（、。？！等）。\n\n"
    "【輸出格式 — 非常重要】\n"
    "先不要直接輸出 SRT。\n"
    "請先只輸出 JSON 陣列，不要加任何解說，不要加 markdown code fence。\n\n"
    "每個元素格式如下：\n"
    "{\n"
    '  "index": 1,\n'
    '  "start": "MM:SS,mmm",\n'
    '  "end": "MM:SS,mmm",\n'
    '  "text": "日語字幕內容"\n'
    "}\n\n"
    "【輸出前檢查】\n"
    "輸出前再次確認：\n"
    "- 所有時間格式都是 MM:SS,mmm\n"
    "- 沒有重疊\n"
    "- 沒有倒退\n"
    "- 每條字幕文字都對應到該時間段實際說出的內容\n"
    "- 斷句自然，不要過碎\n"
)

REFERENCE_SECTION_TEMPLATE = (
    "【參考資料 — 請優先參考】\n"
    "以下是與本音訊相關的參考資料（可能包含人名、地名、專有名詞、術語、"
    "作品名稱、正確用字或背景資訊）。\n"
    "請善用這些資料來：\n"
    "1. 正確辨識並書寫音訊中出現的專有名詞、人名與術語（採用參考資料中的正確寫法）。\n"
    "2. 修正因發音相近而可能誤判的 ASR 錯誤。\n"
    "但請務必注意：\n"
    "- 參考資料只用於「輔助辨識與修正用字」，不可用來憑空捏造音訊中沒有實際說出的內容。\n"
    "- 若參考資料與音訊實際內容衝突，以音訊實際說出的內容為準。\n\n"
    "參考資料內容如下：\n"
    "-----BEGIN REFERENCES-----\n"
    "{references}\n"
    "-----END REFERENCES-----\n\n"
)

RETRY_PROMPT = (
    "你上一次的轉寫不完整，音訊比你轉寫的內容更長。"
    "請重新轉寫整個音訊，務必包含從最開頭到最結尾的所有語音內容，"
    "不要截斷或跳過任何片段。最後一條字幕的時間應對應音訊的完整長度。"
    "請一樣只輸出 JSON 陣列，格式不變。"
)

# ============================================================================
# 提示詞：Stage 2 —— 校對錯字（輸出 JSON）
# ============================================================================
CORRECTION_BASE_PROMPT = (
    "你是一位專業的字幕校對員。我會提供你以下輸入：\n\n"
    "1. **一個音訊檔（FLAC/MP3）** — 這是原始音訊來源\n"
    "2. **一份 JSON 字幕資料** — 這是由較小的語音辨識模型產生的字幕\n\n"
    "## 背景說明\n\n"
    "這份 JSON 字幕資料有以下特性：\n"
    "- **時間軸非常準確**，不需要修改\n"
    "- **但文字錯誤率很高**，包含大量錯字、同音異字、漏字、多字等問題\n\n"
    "## 你的任務\n\n"
    "請你仔細聆聽音訊，逐句比對 JSON 中的文字內容，進行以下校正：\n\n"
    "### ✅ 你應該做的：\n"
    "1. **修正錯字**：將聽到的正確文字替換掉 JSON 中的錯字（例：同音字錯誤、形近字錯誤）\n"
    "2. **修正漏字**：如果某句話少了字詞，請根據音訊補上\n"
    "3. **修正多餘字**：如果出現音訊沒有的字詞，請刪除\n"
    "4. **修正標點符號**：確保標點符號使用正確且自然\n"
    "5. **補回遺漏的整句**：如果音訊中有一整句話被完全遺漏（漏了一整條字幕），請新增該字幕條目，並根據音訊為其標註合理的時間軸\n\n"
    "### ❌ 你絕對不可以做的：\n"
    "1. **不要修改任何現有字幕的時間軸（start / end）** — 開始時間和結束時間必須與原始 JSON 完全一致\n"
    "2. **不要合併或拆分現有的字幕條目**\n"
    "3. **不要重新編排字幕的分段方式**\n"
    "4. **不要對時間軸做任何偏移、微調或「優化」**\n\n"
    "## 輸出要求 — 非常重要\n\n"
    "- **只輸出 JSON 陣列**，不要加任何解說，不要加 markdown code fence。\n"
    "- 每個元素格式如下：\n"
    "{\n"
    '  "index": 1,\n'
    '  "start": "MM:SS,mmm",\n'
    '  "end": "MM:SS,mmm",\n'
    '  "text": "校正後的日語字幕內容"\n'
    "}\n"
    "- index 從 1 開始連續編號\n"
    "- start / end 時間格式與輸入 JSON 保持一致\n"
    "- 不要省略任何字幕條目，必須輸出完整的 JSON 陣列\n\n"
    "## 重要提醒\n\n"
    "你的核心工作是「用耳朵聽音訊，用眼睛看 JSON，修正文字錯誤」。\n"
    "把自己當作一位人工校對員：音訊是標準答案，JSON 是需要批改的考卷。\n"
    "時間軸已經是正確的，請不要動它。\n"
)

CORRECTION_REFERENCE_TEMPLATE = (
    "【參考資料 — 請優先參考】\n"
    "以下是與本音訊相關的參考資料（可能包含人名、地名、專有名詞、術語、"
    "作品名稱、正確用字或背景資訊）。\n"
    "請善用這些資料來修正字幕中的錯字，尤其是專有名詞的用字。\n"
    "若參考資料與音訊實際內容衝突，以音訊實際說出的內容為準。\n\n"
    "參考資料內容如下：\n"
    "-----BEGIN REFERENCES-----\n"
    "{references}\n"
    "-----END REFERENCES-----\n\n"
)

CORRECTION_JSON_INPUT_TEMPLATE = (
    "以下是需要校對的原始 JSON 字幕資料（來自較小語音辨識模型的轉錄結果）：\n"
    "-----BEGIN ORIGINAL JSON-----\n"
    "{original_json}\n"
    "-----END ORIGINAL JSON-----\n\n"
    "請根據音訊校對錯字，並輸出修正後的 JSON 陣列。\n"
)

# ============================================================================
# 全域變數
# ============================================================================
current_api_key_index = 0
CoverageEntry = Tuple[list, float, str, str]

# 當前音檔長度上限（秒），由 process_file 依實際音檔長度更新。
# 供 normalize_time_to_srt 判斷「MM:SS:mmm vs HH:MM:SS」使用。
CURRENT_AUDIO_MAX_SEC = AUDIO_MAX_DURATION_FALLBACK_SEC


def set_audio_duration_context(duration_sec: float) -> None:
    """設定當前音檔長度上限（供時間戳格式判定使用）。"""
    global CURRENT_AUDIO_MAX_SEC
    if duration_sec and duration_sec > 0:
        CURRENT_AUDIO_MAX_SEC = max(float(duration_sec), AUDIO_MAX_DURATION_FALLBACK_SEC)
    else:
        CURRENT_AUDIO_MAX_SEC = AUDIO_MAX_DURATION_FALLBACK_SEC


# ============================================================================
# HDD 保活背景執行緒
# ============================================================================
class HDDKeepAlive:
    """
    背景 daemon 執行緒，每隔 KEEPALIVE_INTERVAL_SEC 秒對工作目錄執行
    一次小型寫入+讀取，防止外接硬碟盒因閒置而自動休眠。
    """

    def __init__(self, directory: Path, interval: int = KEEPALIVE_INTERVAL_SEC):
        self._directory = directory
        self._interval = interval
        self._filepath = directory / KEEPALIVE_FILENAME
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tick_count = 0

    def _keepalive_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._filepath.write_text(
                    f"keepalive {ts} tick={self._tick_count}\n",
                    encoding="utf-8",
                )
                _ = self._filepath.read_text(encoding="utf-8")
                self._tick_count += 1
            except Exception:
                pass
            self._stop_event.wait(timeout=self._interval)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._thread.start()
        print(f"   💽 HDD 保活已啟動（每 {self._interval} 秒寫入/讀取 {KEEPALIVE_FILENAME}）")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            if self._filepath.exists():
                self._filepath.unlink()
        except Exception:
            pass
        print(f"   💽 HDD 保活已停止（共 {self._tick_count} 次 tick）")


# ============================================================================
# ★ v3：VPN / 網路保活背景執行緒
# ============================================================================
class NetworkKeepAlive:
    """
    背景 daemon 執行緒：每隔 NETWORK_KEEPALIVE_INTERVAL_SEC 秒，對輕量端點
    （generate_204，回應無內容）發送一次極小的 HTTP 請求，持續產生網路流量。

    用途：主執行緒在等待 Gemini 長時間思考回應時（generate_content 阻塞），
    該連線上沒有任何封包，VPN 常會判定閒置而斷線，導致後續請求出現
    [Errno -3] DNS 解析失敗。本執行緒用「其他連線的小流量」保住 VPN 活躍。

    ★ 不影響 THINKING_LEVEL / THINKING_BUDGET —— AI 照樣用 high 檔慢慢思考。
    """

    def __init__(self,
                 interval: int = NETWORK_KEEPALIVE_INTERVAL_SEC,
                 timeout: int = NETWORK_KEEPALIVE_TIMEOUT_SEC):
        self._interval = interval
        self._timeout = timeout
        self._urls = list(NETWORK_KEEPALIVE_URLS)
        self._session = requests.Session()
        if NETWORK_KEEPALIVE_PROXIES:
            self._session.proxies.update(NETWORK_KEEPALIVE_PROXIES)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tick_count = 0
        self._fail_count = 0

    def _ping_once(self) -> None:
        url = self._urls[self._tick_count % len(self._urls)]
        try:
            # 204 No Content：只要收到回應就算一次成功流量（幾百 byte 而已）
            self._session.get(url, timeout=self._timeout)
            self._tick_count += 1
        except Exception:
            # VPN 短暫斷線/逾時一律靜默，絕不干擾主流程
            self._fail_count += 1

    def _keepalive_loop(self) -> None:
        while not self._stop_event.is_set():
            self._ping_once()
            self._stop_event.wait(timeout=self._interval)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._thread.start()
        print(f"   🌐 VPN 保活已啟動（每 {self._interval} 秒小封包 → {self._urls[0]}）")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            self._session.close()
        except Exception:
            pass
        print(f"   🌐 VPN 保活已停止（成功 {self._tick_count} 次 / 失敗 {self._fail_count} 次）")


# ============================================================================
# Discord Webhook 通知
# ============================================================================
def send_discord_notification(
    success_files: List[str],
    consistent_files: List[str],
    review_files: List[str],
    failed_files: List[str],
    merged_files: List[str],
    total_files: int,
    elapsed_seconds: float,
) -> None:
    """透過 Discord Webhook 發送處理完成的摘要通知。"""
    if not DISCORD_WEBHOOK or DISCORD_WEBHOOK == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        print("\n⚠️ 未設定 DISCORD_WEBHOOK，跳過 Discord 通知。")
        return

    hours, remainder = divmod(int(elapsed_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        elapsed_str = f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        elapsed_str = f"{minutes}m {seconds}s"
    else:
        elapsed_str = f"{seconds}s"

    def format_file_list(files: List[str], max_show: int = 20) -> str:
        if not files:
            return " (無)\n"
        lines = []
        for f in files[:max_show]:
            lines.append(f" • {f}")
        if len(files) > max_show:
            lines.append(f" … 還有 {len(files) - max_show} 個")
        return "\n".join(lines) + "\n"

    section_success = (
        f"✅ **成功處理 ({len(success_files)} 個):**\n"
        + format_file_list(success_files)
    )
    section_consistent = (
        f"✅ **一致覆蓋率通過 ({len(consistent_files)} 個):**\n"
        + format_file_list(consistent_files)
    )
    section_merged = (
        f"🔀 **時間軸採用 Stage1 修復 ({len(merged_files)} 個):**\n"
        + format_file_list(merged_files)
    )
    section_review = (
        f"⚠️ **建議手動檢查 ({len(review_files)} 個):**\n"
        + format_file_list(review_files)
    )
    section_failed = (
        f"❌ **處理失敗 ({len(failed_files)} 個):**\n"
        + format_file_list(failed_files)
    )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    description = (
        f"📁 共 {total_files} 個檔案 ⏱️ 耗時 {elapsed_str}\n"
        f"🕐 完成時間: {now_str}\n\n"
        f"{section_success}\n"
        f"{section_consistent}\n"
        f"{section_merged}\n"
        f"{section_review}\n"
        f"{section_failed}"
    )

    if failed_files:
        color = 0xFF0000  # 紅色：有失敗
    elif review_files or merged_files:
        color = 0xFFA500  # 橙色：有需檢查 / 有時間軸修復
    else:
        color = 0x00FF00  # 綠色：全部成功

    payload = {
        "embeds": [
            {
                "title": "🎧 字幕生成完成報告",
                "description": description,
                "color": color,
                "footer": {
                    "text": "Audio-to-SRT Generator",
                },
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        ],
    }

    try:
        resp = requests.post(
            DISCORD_WEBHOOK,
            json=payload,
            timeout=30,
        )
        if resp.status_code in (200, 204):
            print("   📨 Discord 通知已發送成功！")
        else:
            print(f"   ⚠️ Discord 通知回傳非預期狀態碼: {resp.status_code}")
            print(f"   回應內容: {resp.text[:300]}")
    except Exception as e:
        print(f"   ⚠️ Discord 通知發送失敗: {e}")


# ============================================================================
# 工具函式
# ============================================================================
def get_next_api_key() -> Tuple[str, int]:
    global current_api_key_index
    api_key = API_KEYS[current_api_key_index]
    key_number = current_api_key_index + 1
    current_api_key_index = (current_api_key_index + 1) % len(API_KEYS)
    return api_key, key_number


def load_references() -> str:
    ref_path = Path.cwd() / REFERENCE_FILENAME
    if not ref_path.exists():
        print(f"   ℹ️ 未找到參考資料檔 ({REFERENCE_FILENAME})，將不使用參考資料。")
        return ""
    try:
        with open(ref_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
    except UnicodeDecodeError:
        try:
            with open(ref_path, 'r', encoding='cp950') as f:
                content = f.read().strip()
        except Exception as e:
            print(f"   ⚠️ 讀取參考資料失敗（編碼問題）: {e}")
            return ""
    except Exception as e:
        print(f"   ⚠️ 讀取參考資料失敗: {e}")
        return ""

    if not content:
        print(f"   ℹ️ 參考資料檔 ({REFERENCE_FILENAME}) 為空，將不使用參考資料。")
        return ""

    print(f"   📖 已讀取參考資料 ({REFERENCE_FILENAME})，共 {len(content)} 字元。")
    return content


def build_transcription_prompt(references: str) -> str:
    if references:
        reference_section = REFERENCE_SECTION_TEMPLATE.format(references=references)
        return reference_section + TRANSCRIPTION_BASE_PROMPT
    return TRANSCRIPTION_BASE_PROMPT


def build_correction_prompt(references: str, original_json: str) -> str:
    parts = []
    if references:
        parts.append(CORRECTION_REFERENCE_TEMPLATE.format(references=references))
    parts.append(CORRECTION_BASE_PROMPT)
    parts.append("\n\n")
    parts.append(CORRECTION_JSON_INPUT_TEMPLATE.format(original_json=original_json))
    return "".join(parts)


def get_model_for_attempt(models: List[str], attempt: int) -> Tuple[str, bool]:
    model_idx = (attempt - 1) // ATTEMPTS_PER_MODEL
    if model_idx >= len(models):
        model_idx = len(models) - 1
    is_switching = (model_idx > 0) and ((attempt - 1) % ATTEMPTS_PER_MODEL == 0)
    return models[model_idx], is_switching


def format_model_chain(models: List[str]) -> str:
    return " → ".join(models)


def is_gemini_2x_model(model: str) -> bool:
    return model.startswith(GEMINI_2X_PREFIXES)


def model_supports_sampling_params(model: str) -> bool:
    return not model.startswith(MODELS_WITHOUT_SAMPLING_PARAMS_PREFIXES)


def get_safety_settings() -> List[types.SafetySetting]:
    categories = [
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        types.HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY,
    ]
    settings = []
    for cat in categories:
        try:
            settings.append(
                types.SafetySetting(
                    category=cat,
                    threshold=types.HarmBlockThreshold.BLOCK_NONE,
                )
            )
        except Exception:
            continue
    return settings


def get_audio_duration(audio_path: str) -> float:
    ext = Path(audio_path).suffix.lower()
    try:
        if ext == ".mp3":
            audio = MP3(audio_path)
            return audio.info.length
        elif ext == ".flac":
            audio = FLAC(audio_path)
            return audio.info.length
        else:
            print(f"   ⚠️ 不支援的格式 {ext}，無法讀取時長")
            return 0.0
    except Exception as e:
        print(f"   ⚠️ 無法讀取音訊時長: {e}")
        return 0.0


def get_mime_type(audio_path: str) -> str:
    ext = Path(audio_path).suffix.lower()
    mime = SUPPORTED_AUDIO_FORMATS.get(ext)
    if mime is None:
        raise ValueError(f"不支援的音訊格式: {ext}，目前支援: {list(SUPPORTED_AUDIO_FORMATS.keys())}")
    return mime


def parse_srt_time(time_str: str) -> float:
    try:
        time_str = time_str.strip()
        if ',' in time_str:
            main_part, ms_part = time_str.split(',')
        else:
            main_part = time_str
            ms_part = "0"
        parts = main_part.split(':')
        if len(parts) == 3:
            h, m, s = parts
        elif len(parts) == 2:
            h = "0"
            m, s = parts
        else:
            return 0.0
        total_seconds = int(h) * 3600 + int(m) * 60 + int(s) + int(ms_part) / 1000.0
        return total_seconds
    except Exception:
        return 0.0


def get_srt_duration(srt_content: str) -> float:
    lines = srt_content.strip().split('\n')
    max_end_time = 0.0
    for line in lines:
        if '-->' in line:
            try:
                parts = line.split('-->')
                if len(parts) == 2:
                    end_time_str = parts[1].strip()
                    end_time = parse_srt_time(end_time_str)
                    if end_time > max_end_time:
                        max_end_time = end_time
            except Exception:
                continue
    return max_end_time


def get_json_duration(json_data: list) -> float:
    max_end = 0.0
    if not isinstance(json_data, list):
        return 0.0
    for item in json_data:
        if not isinstance(item, dict):
            continue
        end_raw = item.get("end", "")
        end_norm = normalize_time_to_srt(end_raw)
        end_sec = parse_srt_time(end_norm)
        if end_sec > max_end:
            max_end = end_sec
    return max_end


# ============================================================================
# ★ 時間戳規範化（v2 修復版）
# ============================================================================
# 修復重點：模型偶發把毫秒用「冒號」接在後面，例如 04:39:458（想表達 4分39.458秒）。
# 舊版會把它誤判成 4小時39分458秒，再補一個假毫秒 → 04:39:458,000（非法）。
# 判定依據：音檔最長 10 分 05 秒（CURRENT_AUDIO_MAX_SEC），
# 按「時:分:秒」解讀會超出音檔長度者，一律改按「分:秒:毫秒」解讀。
#
# 04:39:458 → 00:04:39,458   （第三段 3 位 = 毫秒）
# 03:10:78  → 00:03:10,780   （毫秒右補零）
# 03:32:0   → 00:03:32,000
# 03:22:88  → 00:03:22,880   （秒 = 88 > 59，不可能合法）
# 00:34,500 → 00:00:34,500   （正常 2 段格式，只補小時位）
# 00:02:58  → 00:02:58,000   （合法時:分:秒，維持原解讀）
# ============================================================================
def normalize_time_to_srt(time_str: str) -> str:
    if not isinstance(time_str, str):
        time_str = str(time_str)
    t = time_str.strip().replace('.', ',')
    if not t:
        return "00:00:00,000"

    has_ms = ',' in t
    if has_ms:
        main_part, ms_part = t.split(',', 1)
        ms_part = re.sub(r'\D', '', ms_part)
    else:
        main_part, ms_part = t, ""

    parts = [p for p in main_part.split(':') if p != '']

    if len(parts) == 3:
        a, b, c = parts
        a_d = re.sub(r'\D', '', a) or '0'
        b_d = re.sub(r'\D', '', b) or '0'
        c_d = re.sub(r'\D', '', c) or '0'
        sec_a = int(a_d) * 3600 + int(b_d) * 60 + int(c_d)  # 解讀 A：時:分:秒
        ok_a = (int(b_d) <= 59 and int(c_d) <= 59 and sec_a <= CURRENT_AUDIO_MAX_SEC)

        if len(c_d) == 3 and not has_ms:
            # 情況 A：MM:SS:mmm（毫秒被用冒號接在後面）→ 04:39:458 → 00:04:39,458
            h, m, s, ms_part = "0", a_d, b_d, c_d
        elif not ok_a:
            # 情況 B：音檔不夠長（或秒數 > 59），不可能是 時:分:秒
            # → 改按 分:秒:毫秒 解讀
            h, m, s, ms_part = "0", a_d, b_d, c_d
        else:
            # 合法的 時:分:秒（對 10 分鐘音檔即 00:MM:SS）
            h, m, s = a_d, b_d, c_d
            if not has_ms:
                ms_part = "000"
    elif len(parts) == 2:
        h, m, s = "0", parts[0], parts[1]
    elif len(parts) == 1:
        h, m, s = "0", "0", parts[0]
    else:
        h, m, s = "0", "0", "0"

    try:
        h = int(re.sub(r'\D', '', str(h)) or 0)
        m = int(re.sub(r'\D', '', str(m)) or 0)
        s = int(re.sub(r'\D', '', str(s)) or 0)
    except Exception:
        h, m, s = 0, 0, 0

    ms_part = (re.sub(r'\D', '', str(ms_part)) + "000")[:3]
    return f"{h:02d}:{m:02d}:{s:02d},{ms_part}"


def safe_seconds(raw) -> Optional[float]:
    """時間戳 → 秒（失敗回 None）。內部會先規範化，容錯各種壞格式。"""
    try:
        norm = normalize_time_to_srt(raw)
        return parse_srt_time(norm)
    except Exception:
        return None


def extract_json_from_response(response_text: str) -> str:
    text = response_text.strip()
    fence = "`" * 3
    text = re.sub(r'^' + fence + r'(?:json)?\s*\n?', '', text)
    text = re.sub(r'\n?' + fence + r'$', '', text)
    text = text.strip()

    start = text.find('[')
    end = text.rfind(']')
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text.strip()


def try_load_json(json_text: str) -> Optional[list]:
    try:
        data = json.loads(json_text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except Exception:
        pass

    fixed = json_text
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    open_braces = fixed.count('{')
    close_braces = fixed.count('}')
    if open_braces > close_braces:
        fixed += '}' * (open_braces - close_braces)
    if not fixed.rstrip().endswith(']') and fixed.lstrip().startswith('['):
        fixed = fixed.rstrip().rstrip(',') + ']'

    try:
        data = json.loads(fixed)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except Exception:
        pass
    return None


def json_to_srt(json_text: str) -> str:
    data = try_load_json(json_text)
    if data is None:
        print("   ⚠️ JSON 解析失敗，無法轉換為 SRT")
        return ""

    srt_blocks = []
    idx = 1
    for item in data:
        if not isinstance(item, dict):
            continue
        start_raw = item.get("start", "")
        end_raw = item.get("end", "")
        text = str(item.get("text", "")).strip()
        if start_raw == "" or end_raw == "" or text == "":
            continue
        start_srt = normalize_time_to_srt(start_raw)
        end_srt = normalize_time_to_srt(end_raw)
        block = f"{idx}\n{start_srt} --> {end_srt}\n{text}\n"
        srt_blocks.append(block)
        idx += 1
    return "\n".join(srt_blocks).strip()


def validate_and_fix_srt_format(srt_content: str) -> str:
    srt_content = re.sub(
        r'(\d+:\d+:\d+,\d+),(\d+:\d+:\d+,\d+)',
        r'\1 --> \2',
        srt_content
    )
    srt_content = re.sub(
        r'(\d+:\d+:\d+[,\.]\d+)\s+(\d+:\d+:\d+[,\.]\d+)',
        r'\1 --> \2',
        srt_content
    )
    srt_content = re.sub(
        r'(\d{1,2}:\d{2}:\d{2})\.(\d{3})',
        r'\1,\2',
        srt_content
    )
    srt_content = re.sub(
        r'(\d{1,2}:\d{2})\.(\d{3})',
        r'\1,\2',
        srt_content
    )
    srt_content = re.sub(
        r'(?<![:\d])(\d{1,2}):(\d{2}),(\d{3})',
        r'00:\1:\2,\3',
        srt_content
    )
    # ★ v2：M:SS:mmm（毫秒用冒號）→ 00:MM:SS,mmm
    # 加上 (?:[,\.]\d{1,3})? 把舊版會殘留的多餘毫秒（,000）一併吃掉
    srt_content = re.sub(
        r'(?<![:\d])(\d{1,2}):(\d{2}):(\d{3})(?!\d)(?:[,\.]\d{1,3})?',
        r'00:\1:\2,\3',
        srt_content
    )
    srt_content = re.sub(
        r'(?<![:\d])(\d):(\d{2}):(\d{2}),(\d{3})',
        r'0\1:\2:\3,\4',
        srt_content
    )
    srt_content = re.sub(
        r'\b0+(\d{2}):(\d{2}):(\d{2}),(\d{3})',
        r'\1:\2:\3,\4',
        srt_content
    )

    def pad_time_components(match):
        h = match.group(1).zfill(2)
        m = match.group(2).zfill(2)
        s = match.group(3).zfill(2)
        ms = match.group(4).ljust(3, '0')[:3]
        return f"{h}:{m}:{s},{ms}"

    srt_content = re.sub(
        r'(\d{1,2}):(\d{1,2}):(\d{1,2}),(\d{1,3})',
        pad_time_components,
        srt_content
    )
    # ★ v2：雙毫秒清理 00:04:39,458,000 → 00:04:39,458（最後一道保險）
    srt_content = re.sub(
        r'(\d{1,2}:\d{2}:\d{2}),(\d{3})(?:,\d{1,3})+',
        r'\1,\2',
        srt_content
    )

    lines = srt_content.split('\n')
    fixed_lines = []
    for line in lines:
        if re.search(r'\d{2}:\d{2}:\d{2},\d{3}', line) and '-->' not in line:
            timestamps = re.findall(r'\d{2}:\d{2}:\d{2},\d{3}', line)
            if len(timestamps) >= 2:
                line = f"{timestamps[0]} --> {timestamps[1]}"
        fixed_lines.append(line)
    return '\n'.join(fixed_lines)


# ============================================================================
# ★ 時間軸檢查與 Stage1/Stage2 交叉驗證（fix_srt_json v2 整合）
# ============================================================================
def check_timeline_warnings(data, audio_duration: float) -> List[str]:
    """檢查最終時間軸：倒退（end<start）、重疊、超出音檔長度。"""
    warns: List[str] = []
    prev_end = -1.0
    max_sec = audio_duration if audio_duration and audio_duration > 0 else CURRENT_AUDIO_MAX_SEC
    for it in data:
        if not isinstance(it, dict):
            continue
        idx = it.get('index', '?')
        s = safe_seconds(it.get('start'))
        e = safe_seconds(it.get('end'))
        if s is None or e is None:
            warns.append(f"#{idx} 時間戳無法解析 (start={it.get('start')}, end={it.get('end')})")
            continue
        if e < s:
            warns.append(f"#{idx} end({it['end']}) 早於 start({it['start']})")
        if s < prev_end - 1e-9:
            warns.append(f"#{idx} start({it['start']}) 與前一條重疊")
        prev_end = max(prev_end, e)
        if e > max_sec + 5:
            warns.append(f"#{idx} end({it['end']}) 超出音檔長度上限 {max_sec:.0f}s")
    return warns


def has_inverted_timeline(data) -> bool:
    """是否存在 end < start 的條目（嚴重錯誤，需人工檢查）。"""
    for it in data:
        if not isinstance(it, dict):
            continue
        s = safe_seconds(it.get('start'))
        e = safe_seconds(it.get('end'))
        if s is not None and e is not None and e < s:
            return True
    return False


def pair_entries(stage1: list, stage2: list, tol: float):
    """
    配對 stage1/stage2 條目。
    優先依 index（數量相同且對齊）；否則退回「start 時間最近」配對（貪婪）。
    回傳。
    """
    s1 = sorted([it for it in stage1 if isinstance(it, dict)], key=lambda it: it.get('index') or 0)
    s2 = sorted([it for it in stage2 if isinstance(it, dict)], key=lambda it: it.get('index') or 0)

    if (len(s1) == len(s2)
            and all(a.get('index') == b.get('index') for a, b in zip(s1, s2))):
        return [(b, a) for a, b in zip(s1, s2)], 'index'

    order = sorted(range(len(s1)),
                   key=lambda i: (safe_seconds(s1[i].get('start'))
                                  if safe_seconds(s1[i].get('start')) is not None else -1.0))
    used = set()
    pairs = []
    for it2 in s2:
        t2 = safe_seconds(it2.get('start'))
        best, best_dt = None, None
        if t2 is not None:
            for i in order:
                if i in used:
                    continue
                t1 = safe_seconds(s1[i].get('start'))
                if t1 is None:
                    continue
                dt = abs(t1 - t2)
                if best is None or dt < best_dt:
                    best, best_dt = i, dt
        if best is not None and best_dt <= tol:
            used.add(best)
            pairs.append((it2, s1[best]))
        else:
            pairs.append((it2, None))
    return pairs, 'time'


def cross_validate_merge(stage1_data: list, stage2_data: list, tol: float):
    """
    交叉驗證 stage1 vs stage2 的時間軸。
    - 一致 → 直接用 stage2（結果相同）
    - 衝突 → 採用 stage1 的時間軸 + stage2 的文字
      （stage1 模型時間軸較準，stage2 文字錯字較少 → 各取所長）
    回傳。
    """
    # 先把兩邊的時間戳都規範化，避免「同一時間、不同寫法」被誤判為衝突
    def norm_item(it):
        item = dict(it)
        for k in ('start', 'end'):
            item[k] = normalize_time_to_srt(item.get(k, ''))
        return item

    s1n = [norm_item(it) for it in stage1_data if isinstance(it, dict)]
    s2n = [norm_item(it) for it in stage2_data if isinstance(it, dict)]

    pairs, mode = pair_entries(s1n, s2n, tol)

    merged, conflicts, unmatched = [], [], []
    matched1 = 0
    for it2, it1 in pairs:
        item = dict(it2)
        if it1 is not None:
            matched1 += 1
            for k in ('start', 'end'):
                v1, v2 = it1.get(k), it2.get(k)
                t1, t2 = safe_seconds(v1), safe_seconds(v2)
                if t1 is not None and t2 is not None:
                    differs = abs(t1 - t2) > 1e-6
                else:
                    differs = (v1 != v2)
                if differs:
                    conflicts.append((it2.get('index'), k, v2, v1))
                item[k] = v1  # ★ 一律採用 stage1 時間軸
        else:
            unmatched.append(it2.get('index'))
        merged.append(item)

    dropped = len(s1n) - matched1
    return merged, conflicts, unmatched, dropped, mode


# ============================================================================
# 異常覆蓋率紀錄
# ============================================================================
def save_error_record(
    audio_name: str,
    stage: str,
    attempt: int,
    model: str,
    coverage_ratio: float,
    audio_duration: float,
    json_duration: float,
    json_data: list,
) -> None:
    error_path = Path.cwd() / ERROR_JSON_FILENAME
    records = []
    if error_path.exists():
        try:
            with open(error_path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                records = loaded
        except Exception as e:
            print(f"   ⚠️ 讀取既有 {ERROR_JSON_FILENAME} 失敗，將重新建立: {e}")
            records = []

    records.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file": audio_name,
        "stage": stage,
        "attempt": attempt,
        "model": model,
        "coverage_ratio": round(coverage_ratio, 4),
        "audio_duration_sec": round(audio_duration, 3),
        "subtitle_duration_sec": round(json_duration, 3),
        "subtitle_count": len(json_data) if isinstance(json_data, list) else 0,
        "subtitles": json_data,
    })

    try:
        with open(error_path, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(f"   🧾 已將異常結果 (覆蓋率 {coverage_ratio:.2%}) 寫入 {ERROR_JSON_FILENAME}"
              f"（目前共 {len(records)} 筆）")
    except Exception as e:
        print(f"   ⚠️ 寫入 {ERROR_JSON_FILENAME} 失敗: {e}")


# ============================================================================
# 一致覆蓋率判定（跨模型連續累計）
# ============================================================================
def check_consistent_coverage(
    history: List[CoverageEntry]
) -> Optional[CoverageEntry]:
    if not ACCEPT_CONSISTENT_COVERAGE:
        return None
    if len(history) < CONSISTENT_COVERAGE_COUNT:
        return None
    recent = history[-CONSISTENT_COVERAGE_COUNT:]
    ratios = [entry[1] for entry in recent]
    mean = sum(ratios) / len(ratios)
    if all(abs(r - mean) <= CONSISTENT_COVERAGE_TOLERANCE for r in ratios):
        return min(recent, key=lambda x: abs(x[1] - 1.0))
    return None


def call_gemini_api(
    client: genai.Client,
    model: str,
    contents: list,
    thinking_level: str
) -> Optional[str]:
    internal_client = client
    transient_retries = 0
    while True:
        try:
            if is_gemini_2x_model(model):
                thinking_config = types.ThinkingConfig(
                    thinking_budget=THINKING_BUDGET
                )
            else:
                thinking_config = types.ThinkingConfig(
                    thinking_level=thinking_level
                )

            config_kwargs = dict(
                thinking_config=thinking_config,
                safety_settings=get_safety_settings(),
                response_mime_type="application/json",
                response_json_schema=SUBTITLE_JSON_SCHEMA,
            )
            if model_supports_sampling_params(model):
                config_kwargs["temperature"] = TEMPERATURE
                config_kwargs["top_p"] = TOP_P
            if MEDIA_RESOLUTION_HIGH:
                config_kwargs["media_resolution"] = (
                    types.MediaResolution.MEDIA_RESOLUTION_HIGH
                )

            response = internal_client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            return response.text
        except Exception as e:
            error_str = str(e)
            is_overloaded = '503 UNAVAILABLE' in error_str or 'The model is overloaded' in error_str
            is_dns_error = '[Errno -3] Temporary failure in name resolution' in error_str

            if is_overloaded or is_dns_error:
                transient_retries += 1
                error_type = "模型超載 (503)" if is_overloaded else "網路解析錯誤"
                if transient_retries > MAX_TRANSIENT_RETRIES:
                    print(f"   ❌ {error_type} 已連續重試 {MAX_TRANSIENT_RETRIES} 次仍失敗，"
                          f"放棄此次呼叫（將計入外部重試次數）。")
                    return None
                print(f"   🚦 API 遇到臨時問題 ({error_type})。"
                      f"臨時重試 {transient_retries}/{MAX_TRANSIENT_RETRIES}（不計入外部重試次數）。")
                api_key, key_number = get_next_api_key()
                print(f"   ↪️ 切換至 API Key #{key_number} 並在 {RETRY_DELAY} 秒後重試...")
                internal_client = genai.Client(api_key=api_key)
                time.sleep(RETRY_DELAY)
                continue
            else:
                print(f"   ⚠️ API 呼叫失敗: {e}")
                return None


# ============================================================================
# Stage 1: 轉錄
# ============================================================================
def stage1_transcribe(
    audio_part: types.Part,
    audio_duration: float,
    transcription_prompt: str,
    audio_name: str,
) -> Tuple[str, Optional[list], float, str]:
    print("   ─────────── Stage 1: 轉錄 ───────────")

    candidates: List[CoverageEntry] = []
    coverage_history: List[CoverageEntry] = []

    for attempt in range(1, STAGE1_MAX_RETRIES + 1):
        try:
            current_model, is_switching = get_model_for_attempt(TRANSCRIPTION_MODELS, attempt)
            current_thinking = THINKING_LEVEL

            if is_switching:
                print(f"   🔀 Stage1 前一個模型全部失敗，切換至備援模型: {current_model}")
                if coverage_history:
                    print(f"   ℹ️ 一致性歷史保留 {len(coverage_history)} 筆（跨模型累計，不歸零）")

            print(f"   🔄 Stage1 嘗試 {attempt}/{STAGE1_MAX_RETRIES} (模型: {current_model})")

            api_key, key_number = get_next_api_key()
            print(f"   🔑 使用 API Key #{key_number}")
            client = genai.Client(api_key=api_key)

            contents = [transcription_prompt, audio_part]
            response_text = call_gemini_api(client, current_model, contents, current_thinking)

            if response_text is None:
                coverage_history = []
                if attempt < STAGE1_MAX_RETRIES:
                    print(f"   ⏳ 等待 {RETRY_DELAY} 秒後重試...")
                    time.sleep(RETRY_DELAY)
                continue

            json_text = extract_json_from_response(response_text)
            json_data = try_load_json(json_text)

            if not json_data:
                print(f"   ⚠️ Stage1 JSON 解析失敗，將重試")
                coverage_history = []
                if attempt < STAGE1_MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            if not (CHECK_SUBTITLE_LENGTH and audio_duration > 0):
                print(f"   ✅ Stage1 完成（未啟用長度檢查）")
                return 'SUCCESS', json_data, 1.0, json_text

            json_duration = get_json_duration(json_data)
            coverage_ratio = json_duration / audio_duration
            print(f"   📊 Stage1 覆蓋率: {coverage_ratio:.2%} "
                  f"(音訊: {audio_duration:.1f}s, 字幕: {json_duration:.1f}s)")

            if MIN_COVERAGE_RATIO <= coverage_ratio <= MAX_COVERAGE_RATIO:
                print(f"   ✅ Stage1 覆蓋率正常")
                return 'SUCCESS', json_data, coverage_ratio, json_text

            if coverage_ratio >= ERROR_COVERAGE_RATIO:
                print(f"   ❌ Stage1 覆蓋率嚴重異常 (>= {ERROR_COVERAGE_RATIO:.0%})，"
                      f"此結果不採用")
                save_error_record(
                    audio_name, "stage1", attempt, current_model,
                    coverage_ratio, audio_duration, json_duration, json_data,
                )
                coverage_history = []
                if attempt < STAGE1_MAX_RETRIES:
                    print(f"   ⏳ 等待 {RETRY_DELAY} 秒後進行下一次外部重試...")
                    time.sleep(RETRY_DELAY)
                continue

            print(f"   ⚠️ Stage1 覆蓋率異常")

            if ENABLE_INTERNAL_RETRY:
                print(f"   🔁 執行 Stage1 內部追問...")
                retry_contents = [transcription_prompt, audio_part, response_text, RETRY_PROMPT]
                retry_response_text = call_gemini_api(
                    client, current_model, retry_contents, current_thinking
                )
                if retry_response_text is not None:
                    retry_json_text = extract_json_from_response(retry_response_text)
                    retry_json_data = try_load_json(retry_json_text)
                    if retry_json_data:
                        retry_duration = get_json_duration(retry_json_data)
                        retry_ratio = retry_duration / audio_duration
                        print(f"   📊 追問後覆蓋率: {retry_ratio:.2%}")
                        if MIN_COVERAGE_RATIO <= retry_ratio <= MAX_COVERAGE_RATIO:
                            print(f"   ✅ 追問成功")
                            return 'SUCCESS', retry_json_data, retry_ratio, retry_json_text
                        elif retry_ratio >= ERROR_COVERAGE_RATIO:
                            print(f"   ❌ 追問結果覆蓋率嚴重異常，不採用")
                            save_error_record(
                                audio_name, "stage1-internal-retry", attempt, current_model,
                                retry_ratio, audio_duration, retry_duration, retry_json_data,
                            )
                        elif abs(retry_ratio - 1.0) < abs(coverage_ratio - 1.0):
                            json_data = retry_json_data
                            coverage_ratio = retry_ratio
                            json_text = retry_json_text

            entry: CoverageEntry = (json_data, coverage_ratio, json_text, current_model)
            candidates.append(entry)
            coverage_history.append(entry)
            print(f"   📈 一致性累計: {len(coverage_history)}/{CONSISTENT_COVERAGE_COUNT} 次"
                  f"（跨模型）")

            consistent = check_consistent_coverage(coverage_history)
            if consistent is not None:
                recent = coverage_history[-CONSISTENT_COVERAGE_COUNT:]
                ratios_str = " / ".join(f"{e[1]:.2%}" for e in recent)
                models_used = sorted(set(e[3] for e in recent))
                models_str = ", ".join(models_used)
                print(f"   ✅ Stage1 連續 {CONSISTENT_COVERAGE_COUNT} 次覆蓋率一致 "
                      f"({ratios_str}，容差 ±{CONSISTENT_COVERAGE_TOLERANCE:.0%}；"
                      f"使用模型: {models_str})，判定合格")
                c_data, c_ratio, c_text, _ = consistent
                return 'SUCCESS_CONSISTENT', c_data, c_ratio, c_text

            if attempt < STAGE1_MAX_RETRIES:
                print(f"   ⏳ 等待 {RETRY_DELAY} 秒後進行下一次外部重試...")
                time.sleep(RETRY_DELAY)

        except Exception as e:
            print(f"   ❌ Stage1 嘗試 {attempt} 發生異常: {e}")
            coverage_history = []
            if attempt < STAGE1_MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    if candidates:
        best = min(candidates, key=lambda x: abs(x[1] - 1.0))
        best_data, best_ratio, best_text, best_model = best
        print(f"   💾 Stage1 採用最佳候選 (覆蓋率: {best_ratio:.2%}, 模型: {best_model})")
        if MIN_COVERAGE_RATIO <= best_ratio <= MAX_COVERAGE_RATIO:
            return 'SUCCESS', best_data, best_ratio, best_text
        else:
            return 'SUCCESS_NEEDS_REVIEW', best_data, best_ratio, best_text

    print("   ❌ Stage1 所有嘗試均失敗")
    return 'FAILURE', None, 0.0, ""


# ============================================================================
# Stage 2: 校對錯字
# ============================================================================
def stage2_correct(
    audio_part: types.Part,
    original_json_data: list,
    references: str,
) -> Tuple[str, Optional[list]]:
    print("   ─────────── Stage 2: 校對錯字 ───────────")

    try:
        original_json_str = json.dumps(original_json_data, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"   ❌ Stage2 無法序列化原始 JSON: {e}")
        return 'FAILURE', None

    correction_prompt = build_correction_prompt(references, original_json_str)

    for attempt in range(1, STAGE2_MAX_RETRIES + 1):
        try:
            current_model, is_switching = get_model_for_attempt(CORRECTION_MODELS, attempt)
            current_thinking = THINKING_LEVEL

            if is_switching:
                print(f"   🔀 Stage2 前一個模型全部失敗，切換至備援模型: {current_model}")

            print(f"   🔄 Stage2 嘗試 {attempt}/{STAGE2_MAX_RETRIES} (模型: {current_model})")

            api_key, key_number = get_next_api_key()
            print(f"   🔑 使用 API Key #{key_number}")
            client = genai.Client(api_key=api_key)

            contents = [correction_prompt, audio_part]
            response_text = call_gemini_api(client, current_model, contents, current_thinking)

            if response_text is None:
                if attempt < STAGE2_MAX_RETRIES:
                    print(f"   ⏳ 等待 {RETRY_DELAY} 秒後重試...")
                    time.sleep(RETRY_DELAY)
                continue

            json_text = extract_json_from_response(response_text)
            corrected_data = try_load_json(json_text)

            if not corrected_data:
                print(f"   ⚠️ Stage2 JSON 解析失敗，將重試")
                if attempt < STAGE2_MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            valid_items = [
                it for it in corrected_data
                if isinstance(it, dict) and it.get("text") and it.get("start") and it.get("end")
            ]

            if not valid_items:
                print(f"   ⚠️ Stage2 輸出無有效字幕條目，將重試")
                if attempt < STAGE2_MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            print(f"   ✅ Stage2 校對完成 (共 {len(valid_items)} 條字幕)")
            return 'SUCCESS', valid_items

        except Exception as e:
            print(f"   ❌ Stage2 嘗試 {attempt} 發生異常: {e}")
            if attempt < STAGE2_MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    print("   ❌ Stage2 所有嘗試均失敗")
    return 'FAILURE', None


# ============================================================================
# 主流程：處理單一音訊檔案
# ============================================================================
def process_file(
    audio_path: str,
    srt_path: str,
    audio_duration: float,
    transcription_prompt: str,
    references: str,
    mime_type: str,
) -> Tuple[str, int]:
    """回傳。conflicts_count > 0 表示時間軸曾採用 stage1 修復。"""
    audio_name = os.path.basename(audio_path)
    print(f"   ▶️ 開始處理: {audio_name}")
    print(f"   🎵 MIME type: {mime_type}")

    # ★ 設定時間戳判定用的音檔長度上限（必須在 Stage1 之前，
    #   因為覆蓋率計算的 get_json_duration 內部也會用到 normalize）
    set_audio_duration_context(audio_duration)

    try:
        with open(audio_path, 'rb') as f:
            audio_bytes = f.read()
    except Exception as e:
        print(f"   ❌ 無法讀取音訊檔案: {e}")
        return 'FAILURE', 0

    audio_part = types.Part.from_bytes(
        data=audio_bytes,
        mime_type=mime_type,
    )

    # ------- Stage 1: 轉錄 -------
    s1_status, s1_json_data, s1_ratio, s1_json_text = stage1_transcribe(
        audio_part, audio_duration, transcription_prompt, audio_name
    )

    if s1_status == 'FAILURE' or s1_json_data is None:
        return 'FAILURE', 0

    if SAVE_INTERMEDIATE_JSON:
        intermediate_path = Path(srt_path).with_suffix('.stage1.json')
        try:
            with open(intermediate_path, 'w', encoding='utf-8') as f:
                json.dump(s1_json_data, f, ensure_ascii=False, indent=2)
            print(f"   💾 已儲存 Stage1 中間 JSON: {intermediate_path.name}")
        except Exception as e:
            print(f"   ⚠️ 儲存 Stage1 JSON 失敗: {e}")

    conflicts_count = 0

    # ------- Stage 2: 校對（可選）-------
    if ENABLE_STAGE2:
        s2_status, s2_json_data = stage2_correct(audio_part, s1_json_data, references)

        if s2_status == 'FAILURE' or s2_json_data is None:
            print(f"   ⚠️ Stage2 失敗，回退使用 Stage1 結果輸出 SRT")
            final_json_data = s1_json_data
        else:
            if SAVE_INTERMEDIATE_JSON:
                corrected_path = Path(srt_path).with_suffix('.stage2.json')
                try:
                    with open(corrected_path, 'w', encoding='utf-8') as f:
                        json.dump(s2_json_data, f, ensure_ascii=False, indent=2)
                    print(f"   💾 已儲存 Stage2 校對後 JSON: {corrected_path.name}")
                except Exception as e:
                    print(f"   ⚠️ 儲存 Stage2 JSON 失敗: {e}")

            # ------- ★ Stage1 / Stage2 時間軸交叉驗證 -------
            merged, conflicts, unmatched, dropped, mode = cross_validate_merge(
                s1_json_data, s2_json_data, TIMELINE_MATCH_TOL_SEC
            )
            conflicts_count = len(conflicts)

            if conflicts:
                print(f"   🔀 交叉驗證（配對模式: {mode}）: 時間軸不一致 {len(conflicts)} 筆 "
                      f"→ 採用 stage1 時間軸 + stage2 文字")
                for idx, key, v2v, v1v in conflicts[:MAX_CONFLICT_PRINT]:
                    print(f"      #{idx} {key:<5} stage2: {v2v} → stage1: {v1v}")
                if len(conflicts) > MAX_CONFLICT_PRINT:
                    print(f"      … 其餘 {len(conflicts) - MAX_CONFLICT_PRINT} 筆省略")

                final_json_data = merged

                if SAVE_MERGED_JSON:
                    merged_path = Path(srt_path).with_suffix('.merged.json')
                    try:
                        with open(merged_path, 'w', encoding='utf-8') as f:
                            json.dump(final_json_data, f, ensure_ascii=False, indent=2)
                        print(f"   💾 已儲存合併結果: {merged_path.name}")
                    except Exception as e:
                        print(f"   ⚠️ 儲存合併 JSON 失敗: {e}")
            else:
                print(f"   🔀 交叉驗證（配對模式: {mode}）: 兩階段時間軸完全一致 ✅")
                final_json_data = s2_json_data

            if unmatched:
                show = ", ".join(f"#{i}" for i in unmatched[:10])
                if len(unmatched) > 10:
                    show += f" …等共 {len(unmatched)} 筆"
                print(f"   ⚠️ {len(unmatched)} 筆在 stage1 找不到對應（stage2 補回的漏句），"
                      f"保留 stage2 時間軸: {show}")

            if dropped:
                print(f"   ⚠️ stage1 有 {dropped} 筆未被 stage2 對應（可能被 stage2 刪除）")
    else:
        print("   ⏭️ 已停用 Stage 2 (ENABLE_STAGE2 = False)，直接使用 Stage1 結果輸出 SRT")
        final_json_data = s1_json_data

    # ------- ★ 最終時間軸檢查 -------
    timeline_warns = check_timeline_warnings(final_json_data, audio_duration)
    if timeline_warns:
        print(f"   ⚠️ 最終時間軸檢查發現 {len(timeline_warns)} 項問題:")
        for w in timeline_warns[:MAX_CONFLICT_PRINT]:
            print(f"      - {w}")
        if len(timeline_warns) > MAX_CONFLICT_PRINT:
            print(f"      … 其餘 {len(timeline_warns) - MAX_CONFLICT_PRINT} 項省略")

        if has_inverted_timeline(final_json_data):
            if s1_status.startswith('SUCCESS'):
                print(f"   ⚠️ 偵測到 end < start 的條目，降級為『需要人工檢查』")
                s1_status = 'SUCCESS_NEEDS_REVIEW'

    # ------- 最後：JSON -> SRT -------
    final_json_text = json.dumps(final_json_data, ensure_ascii=False)
    srt_content = json_to_srt(final_json_text)

    if not srt_content.strip():
        print(f"   ❌ 無法將最終 JSON 轉換為 SRT")
        return 'FAILURE', conflicts_count

    srt_content = validate_and_fix_srt_format(srt_content)

    try:
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write(srt_content)
        print(f"   ✅ 已輸出 SRT: {os.path.basename(srt_path)}")
    except Exception as e:
        print(f"   ❌ 寫入 SRT 檔失敗: {e}")
        return 'FAILURE', conflicts_count

    return s1_status, conflicts_count


# ============================================================================
# 主函式
# ============================================================================
def main():
    print("=" * 70)
    if ENABLE_STAGE2:
        print("音訊轉 SRT 字幕生成器 - 雙階段流程 (轉錄 + 校對 + 交叉驗證)")
    else:
        print("音訊轉 SRT 字幕生成器 - 單階段流程 (僅轉錄)")
    print("=" * 70)

    if not API_KEYS or any(key.startswith("YOUR_API_KEY") for key in API_KEYS):
        print("\n❌ 錯誤：請先在腳本頂部設定您的 Google Gemini API Keys")
        return

    if not TRANSCRIPTION_MODELS:
        print("\n❌ 錯誤：TRANSCRIPTION_MODELS 不可為空")
        return

    if ENABLE_STAGE2 and not CORRECTION_MODELS:
        print("\n❌ 錯誤：已啟用 Stage2 但 CORRECTION_MODELS 為空")
        return

    if ACCEPT_CONSISTENT_COVERAGE and CONSISTENT_COVERAGE_COUNT > STAGE1_MAX_RETRIES:
        print(f"\n⚠️ 警告：CONSISTENT_COVERAGE_COUNT ({CONSISTENT_COVERAGE_COUNT}) "
              f"大於 Stage1 總嘗試次數 ({STAGE1_MAX_RETRIES})，一致覆蓋率規則永遠不會觸發。")

    current_dir = Path.cwd()

    # ── 啟動 HDD 保活 ──
    keepalive = HDDKeepAlive(current_dir)
    keepalive.start()

    # ── ★ v3：啟動 VPN / 網路保活 ──
    net_keepalive = NetworkKeepAlive()
    net_keepalive.start()

    # ── 記錄開始時間 ──
    start_time = time.monotonic()

    audio_files = sorted(
        [f for f in current_dir.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_AUDIO_FORMATS],
        key=lambda x: x.name
    )

    if not audio_files:
        print(f"\n❌ 當前目錄下沒有找到支援的音訊檔案（{', '.join(SUPPORTED_AUDIO_FORMATS.keys())}）")
        keepalive.stop()
        net_keepalive.stop()
        return

    format_counts = {}
    for f in audio_files:
        ext = f.suffix.lower()
        format_counts[ext] = format_counts.get(ext, 0) + 1
    format_summary = "、".join(f"{ext} × {cnt}" for ext, cnt in sorted(format_counts.items()))

    print("\n📚 載入參考資料...")
    references = load_references()
    transcription_prompt = build_transcription_prompt(references)

    print(f"\n📁 找到 {len(audio_files)} 個音訊檔案（{format_summary}）")
    print(f"🔑 已加載 {len(API_KEYS)} 個 API Keys")
    print(f"🔧 設定：")
    print(f"   - 支援格式: {', '.join(SUPPORTED_AUDIO_FORMATS.keys())}")
    print(f"   - Stage1 轉錄模型鏈: {format_model_chain(TRANSCRIPTION_MODELS)}")
    print(f"     （共 {len(TRANSCRIPTION_MODELS)} 個模型 × {ATTEMPTS_PER_MODEL} 次 = 最多 {STAGE1_MAX_RETRIES} 次嘗試）")
    if ENABLE_STAGE2:
        print(f"   - Stage2 校對模型鏈: {format_model_chain(CORRECTION_MODELS)}")
        print(f"     （共 {len(CORRECTION_MODELS)} 個模型 × {ATTEMPTS_PER_MODEL} 次 = 最多 {STAGE2_MAX_RETRIES} 次嘗試）")
    else:
        print(f"   - Stage2 校對: ❌ 已停用 (ENABLE_STAGE2 = False)")
    print(f"   - 503/網路臨時錯誤重試上限: {MAX_TRANSIENT_RETRIES} 次（每次呼叫）")
    if is_gemini_2x_model(TRANSCRIPTION_MODELS[0]):
        print(f"   - Thinking Budget: {THINKING_BUDGET} tokens (Gemini 2.x)")
    else:
        print(f"   - Thinking Level: {THINKING_LEVEL} (Gemini 3.x+)")
    print(f"   - Media Resolution: {'HIGH' if MEDIA_RESOLUTION_HIGH else '預設'}")
    print(f"   - 安全設定: 全部關閉")
    if ENABLE_STAGE2:
        print(f"   - 輸出格式: JSON -> JSON(校正) -> 交叉驗證 -> SRT")
    else:
        print(f"   - 輸出格式: JSON -> SRT")
    print(f"   - 內部內容重試: {'啟用' if ENABLE_INTERNAL_RETRY else '停用'}")
    print(f"   - 字幕長度檢查 (Stage1): {'啟用' if CHECK_SUBTITLE_LENGTH else '停用'}")
    if CHECK_SUBTITLE_LENGTH:
        print(f"     - 覆蓋率正常範圍: {MIN_COVERAGE_RATIO:.0%} - {MAX_COVERAGE_RATIO:.0%}")
        if ACCEPT_CONSISTENT_COVERAGE:
            print(f"     - 一致覆蓋率通過: 跨模型連續 {CONSISTENT_COVERAGE_COUNT} 次，"
                  f"容差 ±{CONSISTENT_COVERAGE_TOLERANCE:.0%}（切換模型不歸零）")
        else:
            print(f"     - 一致覆蓋率通過: 停用")
        print(f"     - 異常覆蓋率門檻: >= {ERROR_COVERAGE_RATIO:.0%}（寫入 {ERROR_JSON_FILENAME}，不採用）")
    print(f"   - ★ 時間戳修復: 啟用（04:39:458 → 00:04:39,458 等壞格式自動導正）")
    print(f"   - ★ 音檔長度上限（時間戳判定用）: {AUDIO_MAX_DURATION_FALLBACK_SEC:g}s")
    if ENABLE_STAGE2:
        print(f"   - ★ Stage1/Stage2 時間軸交叉驗證: 啟用"
              f"（配對容差 {TIMELINE_MATCH_TOL_SEC:g}s，衝突時採用 stage1 時間軸 + stage2 文字）")
        print(f"   - ★ 衝突時另存 .merged.json: {'是' if SAVE_MERGED_JSON else '否'}")
    print(f"   - 保留中間 JSON: {'是' if SAVE_INTERMEDIATE_JSON else '否'}")
    print(f"   - 參考資料: {'已載入 (' + REFERENCE_FILENAME + ')' if references else '未使用'}")
    print(f"   - HDD 保活: 每 {KEEPALIVE_INTERVAL_SEC} 秒 ({KEEPALIVE_FILENAME})")
    # ★ v3：VPN 保活狀態
    print(f"   - ★ VPN 保活: 每 {NETWORK_KEEPALIVE_INTERVAL_SEC} 秒小封包（{NETWORK_KEEPALIVE_URLS[0]}）")
    print(f"   - Discord 通知: {'已設定' if DISCORD_WEBHOOK and DISCORD_WEBHOOK != 'YOUR_DISCORD_WEBHOOK_URL_HERE' else '未設定'}")
    print()

    success_files = []
    consistent_files = []
    review_files = []
    failed_files = []
    merged_files = []

    for idx, audio_path in enumerate(audio_files, 1):
        print(f"\n[{idx}/{len(audio_files)}] 處理檔案: {audio_path.name}")
        print("-" * 70)

        srt_path = audio_path.with_suffix('.srt')
        audio_duration = get_audio_duration(str(audio_path))
        if audio_duration > 0:
            print(f"   ⏱️ 音訊時長: {audio_duration:.1f} 秒")

        try:
            mime_type = get_mime_type(str(audio_path))
        except ValueError as e:
            print(f"   ❌ {e}")
            failed_files.append(audio_path.name)
            continue

        status, conflicts_n = process_file(
            str(audio_path), str(srt_path), audio_duration,
            transcription_prompt, references, mime_type,
        )

        if status == 'SUCCESS':
            success_files.append(audio_path.name)
            print(f"   ✅ 成功: {srt_path.name}")
        elif status == 'SUCCESS_CONSISTENT':
            consistent_files.append(audio_path.name)
            print(f"   ✅ 成功（一致覆蓋率通過）: {srt_path.name}")
        elif status == 'SUCCESS_NEEDS_REVIEW':
            review_files.append(audio_path.name)
            print(f"   ⚠️ 需要檢查: {srt_path.name}")
        else:
            failed_files.append(audio_path.name)
            print(f"   ❌ 失敗: {audio_path.name}")

        if status != 'FAILURE' and conflicts_n > 0:
            merged_files.append(audio_path.name)

    # ── 計算耗時 ──
    elapsed_seconds = time.monotonic() - start_time

    # ── 停止 HDD 保活 ──
    keepalive.stop()

    print("\n" + "=" * 70)
    print("處理完成 - 最終報告")
    print("=" * 70)

    print(f"\n✅ 成功處理 ({len(success_files)} 個):")
    if success_files:
        for filename in success_files:
            print(f"   - {filename}")
    else:
        print("   (無)")

    print(f"\n✅ 一致覆蓋率通過 ({len(consistent_files)} 個):")
    if consistent_files:
        for filename in consistent_files:
            print(f"   - {filename}")
        print(f"\n   說明: 覆蓋率不在 {MIN_COVERAGE_RATIO:.0%}-{MAX_COVERAGE_RATIO:.0%}，"
              f"但（跨模型）連續 {CONSISTENT_COVERAGE_COUNT} 次結果一致（±{CONSISTENT_COVERAGE_TOLERANCE:.0%}），"
              f"多半是音訊結尾有靜音/音樂，可視為正常。")
    else:
        print("   (無)")

    print(f"\n🔀 時間軸採用 Stage1 修復 ({len(merged_files)} 個):")
    if merged_files:
        for filename in merged_files:
            print(f"   - {filename}")
        print(f"\n   說明: Stage2 曾改動/損壞時間軸（或兩階段不一致），"
              f"已自動採用 Stage1 時間軸 + Stage2 文字（各取所長）。")
        print(f"   合併結果另存於對應的 .merged.json，可供審計。")
    else:
        print("   (無)")

    print(f"\n⚠️ 建議手動檢查 ({len(review_files)} 個):")
    if review_files:
        for filename in review_files:
            print(f"   - {filename}")
        print("\n   原因: Stage1 字幕覆蓋率超出正常範圍且各次結果不一致，"
              "或最終時間軸存在 end < start 的條目")
        print(f"   建議: 請使用影片播放器檢查這些檔案的字幕是否完整")
    else:
        print("   (無)")

    print(f"\n❌ 處理失敗 ({len(failed_files)} 個):")
    if failed_files:
        for filename in failed_files:
            print(f"   - {filename}")
        print("\n   建議: 請檢查音訊檔案是否損壞或格式不支援")
    else:
        print("   (無)")

    error_path = current_dir / ERROR_JSON_FILENAME
    if error_path.exists():
        print(f"\n🧾 本目錄存在 {ERROR_JSON_FILENAME}，"
              f"內含覆蓋率 >= {ERROR_COVERAGE_RATIO:.0%} 的異常結果，可供除錯。")

    # ── 發送 Discord 通知 ──
    send_discord_notification(
        success_files=success_files,
        consistent_files=consistent_files,
        review_files=review_files,
        failed_files=failed_files,
        merged_files=merged_files,
        total_files=len(audio_files),
        elapsed_seconds=elapsed_seconds,
    )

    # ── ★ v3：停止 VPN / 網路保活（放在 Discord 通知之後，確保通知送出時 VPN 還活著）──
    net_keepalive.stop()

    print("\n" + "=" * 70)
    print("所有任務已完成！")
    print("=" * 70)


if __name__ == "__main__":
    main()
