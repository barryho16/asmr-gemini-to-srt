# -*- coding: utf-8 -*-
"""
moss_to_srt.py —— 雙擊執行的 MOSS-Transcribe-Diarize 批次轉字幕腳本

流程：
  1. 若不是在 venv 裡執行，自動用 VENV_PYTHON 重新啟動自己
  2. 掃描「本腳本所在資料夾」這一層的 mp3 / wav / flac / mp4
  3. 若旁邊有 References.txt，把裡面的詞當熱詞附加到提示詞
  4. 逐檔轉寫，輸出（同名、覆蓋）：
       xxx.srt          純文字字幕（一定輸出）
       xxx.speaker.srt  帶 [S01] 說話人標籤的字幕（KEEP_SPEAKER_SRT 控制）
       xxx.txt          模型原始輸出（KEEP_RAW_TXT 控制）
  5. 全程輸出同時寫入 _moss_transcribe_log.txt
"""
import os
import re
import sys
import time
import wave
import shutil
import tempfile
import traceback
import subprocess
from pathlib import Path

# ==============================================================================
# ✨ 設定區 ✨
# ==============================================================================
VENV_PYTHON = r"E:\MOSS-Transcribe-Diarize\.venv\Scripts\python.exe"
MODEL_PATH  = r"E:\models\MOSS-Transcribe-Diarize"

AUDIO_EXTS    = {".mp3", ".wav", ".flac", ".mp4"}
HOTWORDS_FILE = "References.txt"          # 熱詞檔（放在腳本旁邊，可有可無）
LOG_FILE      = "_moss_transcribe_log.txt"

# ---- 輸出檔案開關（純文字 xxx.srt 一定會輸出，不受開關影響）----
# True = 額外輸出 xxx.speaker.srt（字幕前帶 [S01] 說話人標籤）
# False = 不輸出
KEEP_SPEAKER_SRT = False

# True = 額外輸出 xxx.txt（模型原始輸出，出問題時方便對照）
# False = 不輸出
KEEP_RAW_TXT = True

# ---- 推論設定 ----
DEVICE = "cuda"          # "cuda" / "cpu"
DTYPE  = "float16"       # RTX 2060 用 float16；若出現亂碼/NaN 改 "bfloat16"
ATTN_CANDIDATES = ["sdpa", "eager"]       # 依序嘗試；eager 只是最後手段

MAX_NEW_TOKENS_PER_MIN = 800              # 每分鐘音訊給多少生成 token
MAX_NEW_TOKENS_MIN     = 2048
MAX_NEW_TOKENS_CAP     = 65536
LONG_AUDIO_WARN_MIN    = 20               # 超過此分鐘數提示「建議先切段」
PROGRESS_INTERVAL_SEC  = 2.0              # 進度顯示更新間隔（秒）

MIN_SEGMENT_SEC   = 0.5                   # 字幕最短顯示時長（修正零時長片段）
REMOVE_EVENT_TAGS = False                 # True = 移除 <|xxx|> 這類聲學事件標記

# 程式結束時是否需要按 Enter 才退出
# True = 需要按 Enter
# False = 直接退出
WAIT_FOR_ENTER_BEFORE_EXIT = True
WAIT_ON_ERROR = True                      # 發生嚴重錯誤（如找不到 venv）時仍停住讓你看報錯
# ==============================================================================


# ------------------------------------------------------------------------------
# 0. 自動切換到 venv 的 Python（必須放在所有重量級 import 之前）
# ------------------------------------------------------------------------------
def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


if not _same_file(sys.executable, VENV_PYTHON):
    if not os.path.isfile(VENV_PYTHON):
        print(f"[錯誤] 找不到 venv Python：{VENV_PYTHON}")
        print("       請修改腳本頂部的 VENV_PYTHON 路徑。")
        input("按 Enter 退出...")
        sys.exit(1)
    os.system("chcp 65001 >nul")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    rc = subprocess.call([VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]], env=env)
    sys.exit(rc)

# 到這裡已經是在 venv 裡了
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class Tee:
    """同時輸出到螢幕與日誌檔"""
    def __init__(self, path):
        self.console = sys.__stdout__
        self.f = open(path, "a", encoding="utf-8")

    def write(self, s):
        self.console.write(s)
        self.f.write(s)

    def flush(self):
        self.console.flush()
        self.f.flush()


def console_only(s: str):
    """只印到螢幕、不寫日誌（用於 \\r 覆寫式進度條）"""
    sys.__stdout__.write(s)
    sys.__stdout__.flush()


# ------------------------------------------------------------------------------
# 1. 工具函式
# ------------------------------------------------------------------------------
FFMPEG = shutil.which("ffmpeg")


def fmt_srt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def load_hotwords(folder: Path):
    p = folder / HOTWORDS_FILE
    if not p.is_file():
        return []
    raw = None
    for enc in ("utf-8-sig", "cp932", "gbk", "big5"):
        try:
            raw = p.read_text(encoding=enc)
            break
        except Exception:
            continue
    if raw is None:
        raw = p.read_text(encoding="utf-8", errors="ignore")
    words = []
    for part in re.split(r"[\r\n,，、;；\t]+", raw):
        w = part.strip()
        if w and not w.startswith("#") and w not in words:
            words.append(w)
    return words


def prepare_audio(src: Path, tmpdir: str, idx: int):
    """回傳 (送給模型的檔案路徑, 時長秒)。有 ffmpeg 就統一轉 16k 單聲道 WAV。"""
    if FFMPEG:
        out = Path(tmpdir) / f"moss_{idx:04d}.wav"
        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(src),
               "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0 or not out.is_file() or out.stat().st_size < 100:
            raise RuntimeError(f"ffmpeg 轉檔失敗：\n{r.stderr[:800]}")
        with wave.open(str(out)) as w:
            dur = w.getnframes() / float(w.getframerate())
        return out, dur

    # 沒有 ffmpeg：直接交給官方 loader（mp4 走 PyAV），只先算時長
    from moss_transcribe_diarize.inference_utils import load_audio_item
    samples = load_audio_item(str(src), 16000)
    return src, len(samples) / 16000.0


# ------------------------------------------------------------------------------
# 2. 解析模型輸出（同時相容「有 [Sxx]」與「沒有 [Sxx]」兩種格式）
# ------------------------------------------------------------------------------
TOKEN_RE = re.compile(r"\[(S\d+)\]|\[(\d+(?:\.\d+)?)\]|([^\[]+|\[)")
EVENT_TAG_RE = re.compile(r"<\|[^|]*\|>")


def parse_transcript(text: str):
    """回傳 (segments, truncated)。segment = dict(start, end, speaker, text)"""
    segs = []
    start, spk, buf, pending_spk = None, None, [], None
    last_end = 0.0

    for m in TOKEN_RE.finditer(text):
        s_tag, ts, txt = m.groups()
        if s_tag:
            if start is None:
                pending_spk = s_tag
            else:
                spk = s_tag
        elif ts:
            t = float(ts)
            if start is None:
                start, spk, buf, pending_spk = t, pending_spk, [], None
            else:
                content = "".join(buf).strip()
                if content:
                    segs.append({"start": start, "end": t, "speaker": spk, "text": content})
                last_end = t
                start, spk, buf = None, None, []
        else:
            if start is not None:
                buf.append(txt)
            elif txt.strip():
                # 模型漏掉起始時間戳 → 用上一段結束時間當起點
                start, spk, buf, pending_spk = last_end, pending_spk, [txt], None

    truncated = False
    if start is not None and "".join(buf).strip():
        content = "".join(buf).strip()
        est = max(1.0, len(content) * 0.15)
        segs.append({"start": start, "end": start + est, "speaker": spk, "text": content})
        truncated = True
    return segs, truncated


def clean_segments(segs):
    for s in segs:
        if REMOVE_EVENT_TAGS:
            s["text"] = EVENT_TAG_RE.sub("", s["text"]).strip()
        if s["end"] < s["start"]:
            s["start"], s["end"] = s["end"], s["start"]
    segs = [s for s in segs if s["text"]]
    segs.sort(key=lambda x: x["start"])

    for s in segs:
        if s["end"] - s["start"] < MIN_SEGMENT_SEC:
            s["end"] = s["start"] + MIN_SEGMENT_SEC

    # 修正相鄰重疊：前一段結束 > 後一段開始
    for i in range(len(segs) - 1):
        cur, nxt = segs[i], segs[i + 1]
        if cur["end"] > nxt["start"] and nxt["start"] - cur["start"] >= MIN_SEGMENT_SEC:
            cur["end"] = nxt["start"]
    return segs


def write_srt(path: Path, segs, with_speaker: bool):
    lines = []
    for i, s in enumerate(segs, 1):
        text = s["text"]
        if with_speaker and s["speaker"]:
            text = f"[{s['speaker']}] {text}"
        lines += [str(i), f"{fmt_srt_time(s['start'])} --> {fmt_srt_time(s['end'])}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def remove_if_exists(path: Path):
    """開關關閉時，順手刪掉上次遺留的同名檔，避免新舊混淆"""
    try:
        if path.is_file():
            path.unlink()
    except Exception:
        pass


# ------------------------------------------------------------------------------
# 3. 模型
# ------------------------------------------------------------------------------
def load_model():
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor

    use_cuda = DEVICE == "cuda" and torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    dtype = getattr(torch, DTYPE) if use_cuda else torch.float32
    print(f"   裝置: {device}  |  精度: {dtype}  |  "
          f"GPU: {torch.cuda.get_device_name(0) if use_cuda else '無'}")

    last_err = None
    model = None
    for attn in ATTN_CANDIDATES:
        try:
            model = AutoModelForCausalLM.from_pretrained(
                MODEL_PATH, trust_remote_code=True, dtype="auto", attn_implementation=attn,
            ).to(dtype=dtype).to(device).eval()
            print(f"   注意力後端: {attn}")
            break
        except Exception as e:
            last_err = e
            print(f"   attn_implementation={attn} 失敗，嘗試下一個…（{type(e).__name__}: {e}）")
    if model is None:
        raise last_err

    processor = AutoProcessor.from_pretrained(MODEL_PATH, trust_remote_code=True)
    return model, processor, device, dtype


def transcribe(model, processor, device, dtype, audio_path: Path, prompt: str, max_new_tokens: int):
    from moss_transcribe_diarize.inference_utils import (
        build_transcription_messages, generate_transcription,
    )

    messages = build_transcription_messages(str(audio_path), prompt=prompt)

    t_start = time.time()
    state = {"last": 0.0}

    def on_token(n):
        now = time.time()
        if now - state["last"] >= PROGRESS_INTERVAL_SEC:
            state["last"] = now
            console_only(f"\r   ⏩ 已生成 {n:>6} / {max_new_tokens} token  |  "
                         f"已耗時 {now - t_start:6.1f}s   ")

    result = generate_transcription(
        model, processor, messages,
        max_new_tokens=max_new_tokens, do_sample=False,
        device=device, dtype=dtype,
        token_callback=on_token,
    )
    console_only("\r" + " " * 70 + "\r")   # 清掉進度行
    return result["text"], result.get("generated_tokens", 0)


# ------------------------------------------------------------------------------
# 4. 主流程
# ------------------------------------------------------------------------------
def main():
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)
    sys.stdout = sys.stderr = Tee(script_dir / LOG_FILE)

    print("\n" + "=" * 66)
    print(f"🎙  MOSS-Transcribe-Diarize 批次轉字幕    {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 66)
    print(f"📁 工作資料夾: {script_dir}")
    print(f"🐍 Python: {sys.executable}")
    print(f"🎞  ffmpeg: {FFMPEG or '未找到（改用 PyAV 直接解碼）'}")
    print(f"📝 輸出: xxx.srt (必)  |  xxx.speaker.srt {'✓' if KEEP_SPEAKER_SRT else '✗'}  |  "
          f"xxx.txt {'✓' if KEEP_RAW_TXT else '✗'}")

    # 找檔案
    files = sorted(
        p for p in script_dir.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    )
    if not files:
        print("\n❌ 資料夾中沒有 mp3 / wav / flac / mp4 檔案。")
        return 0
    print(f"\n🔍 找到 {len(files)} 個檔案:")
    for p in files:
        print(f"   - {p.name}")

    stems = [p.stem for p in files]
    dup = {s for s in stems if stems.count(s) > 1}
    if dup:
        print(f"\n⚠ 以下檔名相同、副檔名不同，輸出會互相覆蓋: {', '.join(sorted(dup))}")

    # 熱詞
    from moss_transcribe_diarize.inference_utils import DEFAULT_PROMPT
    hotwords = load_hotwords(script_dir)
    prompt = DEFAULT_PROMPT
    if hotwords:
        prompt += "热词提示：" + ", ".join(hotwords)
        print(f"\n🔥 已載入 {len(hotwords)} 個熱詞: "
              f"{', '.join(hotwords[:10])}{' …' if len(hotwords) > 10 else ''}")
    else:
        print(f"\nℹ 未找到 {HOTWORDS_FILE}，使用預設提示詞")

    # 載入模型
    print("\n⏳ 載入模型中（第一次約需 30 秒～2 分鐘）…")
    t0 = time.time()
    model, processor, device, dtype = load_model()
    print(f"✓ 模型載入完成，耗時 {time.time() - t0:.1f}s")

    import torch
    ok, fail = 0, 0
    total_audio, total_time = 0.0, 0.0
    tmpdir = tempfile.mkdtemp(prefix="moss_srt_")

    try:
        for idx, src in enumerate(files, 1):
            print("\n" + "─" * 66)
            print(f"[{idx}/{len(files)}] {src.name}")
            print("─" * 66)
            t_file = time.time()
            wav = None
            try:
                wav, dur = prepare_audio(src, tmpdir, idx)
                print(f"   時長: {dur/60:.1f} 分鐘 ({dur:.0f}s)")
                if dur / 60 > LONG_AUDIO_WARN_MIN:
                    print(f"   ⚠ 音訊超過 {LONG_AUDIO_WARN_MIN} 分鐘，6GB 顯存可能不足，"
                          f"建議先用 自動切段.py 切成 10 分鐘片段")

                max_new = int(min(MAX_NEW_TOKENS_CAP,
                                  max(MAX_NEW_TOKENS_MIN, dur / 60 * MAX_NEW_TOKENS_PER_MIN)))
                print(f"   max_new_tokens = {max_new}")
                print("   轉寫中…")

                raw, n_tokens = transcribe(model, processor, device, dtype, wav, prompt, max_new)

                base = src.with_suffix("")
                txt_path = Path(str(base) + ".txt")
                srt_path = Path(str(base) + ".srt")
                spk_path = Path(str(base) + ".speaker.srt")

                segs, truncated = parse_transcript(raw)
                segs = clean_segments(segs)

                # 純文字 srt 一定輸出
                write_srt(srt_path, segs, with_speaker=False)
                outputs = [srt_path.name]

                if KEEP_SPEAKER_SRT:
                    write_srt(spk_path, segs, with_speaker=True)
                    outputs.append(spk_path.name)
                else:
                    remove_if_exists(spk_path)

                if KEEP_RAW_TXT:
                    txt_path.write_text(raw, encoding="utf-8")
                    outputs.append(txt_path.name)
                else:
                    remove_if_exists(txt_path)

                n_spk = len({s["speaker"] for s in segs if s["speaker"]})
                elapsed = time.time() - t_file
                total_audio += dur
                total_time += elapsed
                print(f"   ✓ {len(segs)} 段字幕  |  說話人: {n_spk if n_spk else '未標註(單人)'}  |  "
                      f"生成 {n_tokens} token  |  耗時 {elapsed:.1f}s  |  RTF {elapsed/max(dur,1e-6):.3f}")
                print(f"   → {'  '.join(outputs)}")
                if truncated or n_tokens >= max_new:
                    print("   ⚠ 輸出疑似被截斷（達到 token 上限或最後一段沒有結束時間戳），"
                          "可調高 MAX_NEW_TOKENS_PER_MIN 後重跑此檔")
                if not segs:
                    print("   ⚠ 沒有解析出任何片段，請檢查原始輸出（建議把 KEEP_RAW_TXT 設為 True）")
                ok += 1

            except torch.cuda.OutOfMemoryError:
                fail += 1
                console_only("\r" + " " * 70 + "\r")
                print("   ❌ CUDA 顯存不足 (OOM)。此檔太長，請先用 自動切段.py 切段後再轉。")
            except Exception as e:
                fail += 1
                console_only("\r" + " " * 70 + "\r")
                print(f"   ❌ 失敗: {type(e).__name__}: {e}")
                traceback.print_exc()
            finally:
                if device.type == "cuda":
                    torch.cuda.empty_cache()
                if wav is not None and wav.parent == Path(tmpdir):
                    try:
                        wav.unlink()
                    except Exception:
                        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 66)
    print(f"✅ 全部完成   成功 {ok}  |  失敗 {fail}")
    if total_audio > 0:
        print(f"   音訊總長 {total_audio/60:.1f} 分鐘，處理耗時 {total_time/60:.1f} 分鐘，"
              f"平均 RTF {total_time/total_audio:.3f}")
    print(f"   日誌: {LOG_FILE}")
    print("=" * 66)
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    code = 1
    crashed = False
    try:
        code = main()
    except Exception:
        crashed = True
        print("\n💥 程序發生未捕獲的錯誤:")
        traceback.print_exc()
    finally:
        if WAIT_FOR_ENTER_BEFORE_EXIT or (crashed and WAIT_ON_ERROR):
            try:
                input("\n按 Enter 退出...")
            except Exception:
                pass
    sys.exit(code)