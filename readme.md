# asmr-gemini-to-srt

一套把**音訊自動轉成字幕、再翻譯成繁體中文**的完整流程工具集。

整條流水線可以一鍵執行，也可以只挑其中某一步單獨跑。每一支程式都是獨立的
Python 檔案，彼此用「資料夾」傳遞檔案，因此可以替換掉任何一環（例如把 AI
轉錄換成本機的 MOSS 模型）。

---

## 一、整體流程

```
音訊 (mp3 / wav / flac / mp4)
        │
        ▼
  ① 音訊轉換與切段
     audio_converter_splitter-ai-v3-flac.py
     ↓  converted_audio/*.flac（已濾波 / 壓縮 / 正規化，並依 10 分鐘切段、5 秒重疊）
     │
     ▼
  ② AI 轉錄產生日文 SRT
     converted_audio/gemini-3.1-lite-to-srt.py
     ↓  converted_audio/*.srt（日文）
     │
     ▼
  ③ 參考資料搬移到合併區
     converted_audio/copy_references.py
     ↓  converted_audio/merged_srt/References.txt
     │
     ▼
  ④ 合併各段 SRT（依 part 編號回推時間軸偏移）
     converted_audio/merge_srt_part-Backward-only-Overlap.py
     ↓  converted_audio/merged_srt/<完整音訊>.srt
     │
     ▼
  ⑤ AI 翻譯成中文（自動鎖定原文時間軸）
     converted_audio/merged_srt/nv_glm5.1_srt_to_zh_v3.py
     ↓  converted_audio/merged_srt/translated_srt/*.srt
     │
     ▼
  ⑥ 校驗並鎖定時間軸（選用，純手動翻譯時才需要）
     converted_audio/merged_srt/srt_fix.py
     ↓  converted_audio/merged_srt/final_srt/*.srt
```

### 一鍵執行

```bash
python asmr-gemini-to-srt/start_to_srt_scripts.py
```

`start_to_srt_scripts.py` 會依上表順序 ①→⑤ 逐一支呼叫，並把每支程式的輸出
即時轉發到主控台。它會強制子行程使用 UTF-8 編碼，避免 Windows 主控台出現亂碼。

---

## 二、替代方案：不使用雲端 AI 轉錄

如果你不想把音訊傳到雲端，可以用本機模型產生日文 SRT，
之後從 **④** 之後的步驟接手即可。

```bash
python asmr-gemini-to-srt/converted_audio/moss_to_srt.py
```

| 項目 | 說明 |
| --- | --- |
| 模型 | MOSS-Transcribe-Diarize（可支援說話人分離） |
| 執行環境 | 會自動用 `VENV_PYTHON` 指定的 venv 重新啟動自己 |
| 熱詞 | 會自動讀取同層的 `References.txt` 附加到提示詞 |
| 輸出 | `xxx.srt`（一定輸出）、`xxx.speaker.srt`、`xxx.txt` |
| 日誌 | 全程寫入 `_moss_transcribe_log.txt` |

> 這支程式**不在** `start_to_srt_scripts.py` 的預設流程中，需自行執行。

---

## 三、各步驟詳細說明

### ① 音訊轉換與切段

`audio_converter_splitter-ai-v3-flac.py`

讀取程式所在資料夾的音訊，輸出到 `converted_audio/`。處理順序：

1. **切段**：依 `split_duration_min` 決定何時切分，段落間保留
   `split_overlap_sec` 秒重疊（重疊是為了避免切在句子中間）。
2. **人聲分離**（選用）：用 Audio-Separator 的 MDX23C 模型抽出人聲。
3. **濾波去噪**：高通 50 Hz、低通 12000 Hz，用來去低頻隆隆聲與高頻嘶聲。
4. **動態壓縮**：閾值 -20 dB、比率 3:1、補償增益 6 dB，把音量忽大忽小壓平。
5. **峰值正規化**：把峰值拉到 -1.5 dBFS。
6. **輸出 FLAC**：無損壓縮，AI 讀取時不易產生劣化雜訊。

所有參數集中在檔案上方的 `PROCESSING_CONFIG`，可直接修改。

> 為什麼要留重疊？因為 AI 在重疊區會轉出重複的句子，④ 的合併程式會用
> 時間軸偏移把每段接回正確位置。

### ② AI 轉錄產生日文 SRT

`converted_audio/gemini-3.1-lite-to-srt.py`

呼叫 Gemini API，用**兩階段**流程：

- **Stage 1（轉錄）**：把音訊轉成帶時間戳的日文 SRT。
- **Stage 2（校對）**：交給更強的模型修正錯字、統一術語。
- **交叉驗證**：兩階段的時間軸不一致時，**採用 Stage 1 的時間軸 + Stage 2 的文字**。

另外還包含：

- 時間戳壞格式修復（`04:39:458` → `00:04:39,458`）
- VPN / 網路保活背景執行緒，避免 AI 長時間思考時被判定閒置而斷線
- 失敗自動換模型重試（`TRANSCRIPTION_MODELS` / `CORRECTION_MODELS` 依序嘗試）

### ③ 搬移參考資料

`converted_audio/copy_references.py`

把 `References.txt` 複製到 `merged_srt/`，讓後續步驟能讀到同一份參考資料
（人名、專有名詞、術語表等）。

### ④ 合併切段產生的 SRT

`converted_audio/merge_srt_part-Backward-only-Overlap.py`

把 `xxx_part1.srt`、`xxx_part2.srt`… 合併回一條完整字幕。偏移量公式：

```
Offset(第 N 段) = (N - 1) × 平均時長 - 重疊秒數
```

平均時長從檔名的 `_450000ms` 取出。**`OVERLAP_MS` 必須與 ① 的
`split_overlap_sec` 保持一致**，否則時間軸會整段偏移。

重疊區重複出現的字幕會在合併時自然被保留下來，需要人工確認是否刪除。

### ⑤ AI 翻譯成中文

`converted_audio/merged_srt/nv_glm5.1_srt_to_zh_v3.py`

把日文 SRT 翻成中文（走 NVIDIA / OpenAI 相容的 API）。核心安全機制：

1. 嚴格解析 SRT 格式。
2. 把 `References.txt` 的內容注入提示詞，確保專有名詞一致。
3. AI 回傳後**嚴格校驗**（總筆數與序號必須完全對得上）。
4. **強制鎖定原文時間軸**：直接丟棄 AI 產生的時間軸，只取用原文的時間軸 +
   AI 的譯文重新組裝，避免 AI 把時間改壞。

設定重點在檔案上方：`API_KEYS`、`API_ENDPOINT`、`MODEL_PRIORITY`、
重試次數與延遲。

### ⑥ 校驗並鎖定時間軸（選用）

`converted_audio/merged_srt/srt_fix.py`

**僅在你改用網頁版 AI 手動翻譯時才需要**（此時不會有 ⑤ 的自動校驗）。

1. 把原文 `.srt` 放在程式所在資料夾。
2. 用 ChatGPT / Gemini / Claude 手動翻譯，存成**同檔名**的 `.srt`，
   放進 `translated_srt/`。
3. 執行本程式，結果輸出到 `final_srt/`。

---

## 四、環境需求

| 用途 | 需求 |
| --- | --- |
| 基本執行 | Python 3.9+ |
| ① 音訊處理 | `pip install pydub numpy scipy tqdm`、系統需有 `ffmpeg` / `ffprobe` |
| ① 人聲分離（選用） | `pip install audio-separator`（含 `torch`、`onnxruntime`） |
| ②③④⑤⑥ | `pip install requests mutagen google-genai` |
| ⑤ AI 翻譯 | NVIDIA / OpenAI 相容 API 的 Key |
| 替代轉錄 | 另需 MOSS-Transcribe-Diarize 模型與對應 venv |

### 憑證設定（**必須先做**）

金鑰不會內建，執行前請自行填入對應檔案的上方設定區：

| 檔案 | 需要填入 |
| --- | --- |
| `converted_audio/gemini-3.1-lite-to-srt.py` | `API_KEYS`（Google Gemini 金鑰，可多組輪替）、`DISCORD_WEBHOOK`（選填） |
| `converted_audio/merged_srt/nv_glm5.1_srt_to_zh_v3.py` | `API_KEYS`（NVIDIA / OpenAI 相容金鑰） |
| `converted_audio/moss_to_srt.py` | `VENV_PYTHON`、`MODEL_PATH`（本機路徑） |

範例：

```python
API_KEYS = [
    "YOUR_API_KEY_HERE",
]
DISCORD_WEBHOOK = "YOUR_DISCORD_WEBHOOK_URL_HERE"   # 留著預設值即為停用
```

未設定時程式會直接顯示錯誤訊息並停止，**不會**帶著佔位符去呼叫 API。

Windows 使用者建議先執行 `chcp 65001`，避免主控台輸出亂碼。

---

## 五、資料夾結構

```
asmr-gemini-to-srt/
├── start_to_srt_scripts.py          一鍵執行入口
├── audio_converter_splitter-ai-v3-flac.py
│                                     ① 音訊轉換與切段
└── converted_audio/
    ├── gemini-3.1-lite-to-srt.py    ② AI 轉錄（日文 SRT）
    ├── copy_references.py           ③ 搬移 References.txt
    ├── merge_srt_part-Backward-only-Overlap.py
    │                                     ④ 合併各段 SRT
    ├── moss_to_srt.py                替代方案：本機模型轉錄
    ├── References.txt                參考資料（人名 / 術語表）
    └── merged_srt/
        ├── nv_glm5.1_srt_to_zh_v3.py ⑤ AI 翻譯成中文
        ├── srt_fix.py                ⑥ 手動翻譯版的校驗工具
        ├── References.txt
        ├── translated_srt/           ⑤ 的輸出
        └── final_srt/                ⑥ 的輸出
```

---

## 六、注意事項

- **重疊秒數必須一致**：① 的 `split_overlap_sec` 與 ④ 的 `OVERLAP_MS` 要對齊，
  否則合併後的時間軸會整段偏移。
- **金鑰請勿提交到版控**：設定值是寫在 `.py` 裡的明文。本版已將所有金鑰
  換成佔位符，但填回去後千萬不要連同版控一起分享；若已不小心外流，
  請務必到各家後台**撤銷並重新產生**金鑰。
- **不確定有沒有外流**：可搜尋檔案中的 `AIza`（Google）、`nvapi-`（NVIDIA）、
  `discord.com/api/webhooks`（Discord）確認已無真實金鑰。
- **參考資料影響翻譯品質**：`References.txt` 會被注入提示詞並**優先於**預設譯法，
  請確保內容正確。
- **長音訊耗時**：10 分鐘以上的音訊建議先切段（① 預設 10 分鐘），
  也避免 GPU 顯示記憶體不足。
