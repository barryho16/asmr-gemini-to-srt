import os
import sys

# ⚠️ 關鍵修復：必須在所有匯入之前添加使用者路徑
import site
user_site = site.getusersitepackages()
if user_site not in sys.path:
    sys.path.insert(0, user_site)

# 現在才能正常匯入其他模組
import subprocess
import shutil
import re
import tempfile
from pathlib import Path


# ==============================================================================
# ✨ 程式退出行為設定 ✨
# ==============================================================================
# 程式結束時是否需要按 Enter 才退出
# True = 需要按 Enter
# False = 直接退出
WAIT_FOR_ENTER_BEFORE_EXIT = False
# ==============================================================================


def wait_for_enter(message="按Enter键退出..."):
    """
    根據 WAIT_FOR_ENTER_BEFORE_EXIT 決定是否等待使用者按 Enter。
    - 開關為 False：直接傳回，程式立刻退出
    - 開關為 True ：提示並等待輸入
    同時相容非互動環境（重定向輸入 / 排程任務 / IDE 主控台），
    此時 input() 會拋 EOFError，這裡靜默放行，避免程式卡死或堆疊追蹤。
    """
    if not WAIT_FOR_ENTER_BEFORE_EXIT:
        return
    try:
        input(f"\n{message}")
    except (EOFError, KeyboardInterrupt):
        pass


# 依賴檢查和匯入部分
try:
    from pydub import AudioSegment
    import numpy as np
    from scipy import signal
except ImportError:
    print("错误：未安装必要的库")
    print("请运行：pip install pydub numpy scipy")
    wait_for_enter()
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        print("提示：安装tqdm库 可以获得更好的处理进度条。")
        return iterable

# 檢查 audio-separator 是否安裝
AUDIO_SEPARATOR_AVAILABLE = False
Separator = None
try:
    from audio_separator.separator import Separator
    AUDIO_SEPARATOR_AVAILABLE = True
except ImportError as e:
    print(f"\n[诊断信息] 导入 'audio_separator' 失败。根本原因很可能是缺少深层依赖。")
    print(f"   > 错误详情: {e}")
    pass
except Exception as e:
    print(f"\n[诊断信息] 导入或初始化 'audio_separator' 时发生未知错误。")
    print(f"   > 错误详情: {e}")
    pass

# 除錯：輸出 Python 環境資訊
print(f"🐍 Python 环境诊断:")
print(f"   执行路径: {sys.executable}")
print(f"   版本: {sys.version.split()[0]}")
print(f"   用户 site-packages: {user_site}")
print(f"   系统 site-packages:")
for path in site.getsitepackages():
    print(f"     - {path}")

if AUDIO_SEPARATOR_AVAILABLE:
    print("\n✓ 检测到 audio-separator，将使用 MDX23C 进行人声分离")
    try:
        import torch
        print(f"✓ PyTorch 版本: {torch.__version__}")
        print(f"  CUDA 可用: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"  GPU: {torch.cuda.get_device_name(0)}")
    except ImportError:
        print("⚠ PyTorch 未正确安装（GPU 加速将不可用）")
else:
    print("\n⚠ 未检测到 audio-separator，将跳过人声分离步骤")
    print("  请检查上面的 [诊断信息] 以确定根本原因。")
    print("  常见原因及解决方法：")
    print("  - 错误提示 `No module named 'torch'` -> 请运行: pip install torch")
    print("  - 错误提示 `No module named 'onnxruntime'` -> 请运行: pip install onxruntime")
    print("  - 如果以上都已安装，尝试强制重装: python -m pip install --force-reinstall audio-separator")


# ==============================================================================
# ✨ 硬編碼參數設定中心 ✨
# ==============================================================================
PROCESSING_CONFIG = {
    "FILTER": {
        "highpass_enabled": True,
        "lowpass_enabled": True,
        "highpass_cutoff": 50,       # 高通截止頻率
        "lowpass_cutoff": 12000,     # 低通截止頻率
        "order": 5
    },
    "COMPRESSOR": {
        "enabled": True,
        "threshold_db": -20,
        "ratio": 3.0,
        "makeup_gain_db": 6,
        # ✨✨✨ 新增：小訊號增強開關 ✨✨✨
        # 閾值以下訊號做漸進提升；設為 False 則閾值以下訊號原樣通過
        "below_threshold_boost_enabled": False,
        "below_threshold_boost_ratio": 0.3   # 0.3 = 最多 +30% 漸進提升（可調整 0.0~1.0）
    },
    "NORMALIZATION": {
        "enabled": True,
        "peak_target_dbfs": -1.5
    },
    "OUTPUT": {
        # ✨✨✨ 新增：輸出格式改為 FLAC（無損）✨✨✨
        "output_format": "flac",
        "flac_compression_level": 5,  # FLAC 壓縮等級 0-8（無損！只影響檔案大小和編碼速度）
        # 這個值用來決定 "何時觸發分割" 以及 "分割成幾份"
        "split_duration_min": 10,
        "split_overlap_sec": 5,       # 切割重疊的秒數
        "enable_splitting": True
    },
    # Audio-Separator 設定
    "SEPARATION": {
        "enabled": False,
        "model_name": "MDX23C-8KFFT-InstVoc_HQ",
        "use_cuda": True,
        "denoise": True,
        "normalize": True,
        "output_format": "WAV",
        "segment_size": 512,
        "overlap": 0.25,
        "keep_separated_temp": False
    }
}
# ==============================================================================


# ==============================================================================
# ✨ ffmpeg / ffprobe 工具函式 ✨
# ==============================================================================
def _get_ffmpeg():
    """獲取 ffmpeg 可執行檔路徑"""
    try:
        converter = AudioSegment.converter
        if converter and shutil.which(converter):
            return converter
    except Exception:
        pass
    found = shutil.which('ffmpeg')
    return found or 'ffmpeg'


def _get_ffprobe():
    """獲取 ffprobe 可執行檔路徑"""
    ffmpeg = _get_ffmpeg()
    if ffmpeg and ffmpeg != 'ffmpeg':
        candidate = ffmpeg.replace('ffmpeg', 'ffprobe')
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which('ffprobe')
    return found or 'ffprobe'


FFMPEG = _get_ffmpeg()
FFPROBE = _get_ffprobe()
# ==============================================================================


def get_audio_duration_ms(input_file):
    """使用 ffprobe 獲取音訊時長（毫秒），完全不載入檔案到記憶體"""
    # 方法1：ffprobe
    try:
        cmd = [
            FFPROBE,
            '-v', 'quiet',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            input_file
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            val = result.stdout.strip()
            if val.lower() != 'n/a':
                duration_sec = float(val)
                return int(duration_sec * 1000)
    except FileNotFoundError:
        print("   ⚠ ffprobe 未找到，尝试使用 ffmpeg 获取时长...")
    except Exception as e:
        print(f"   ⚠ ffprobe 获取时长失败: {e}")

    # 方法2：ffmpeg -i（僅讀取頭部，立即退出）
    try:
        cmd = [FFMPEG, '-i', input_file]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
        if match:
            h, m, s = match.groups()
            duration_sec = int(h) * 3600 + int(m) * 60 + float(s)
            return int(duration_sec * 1000)
    except Exception as e:
        print(f"   ⚠ ffmpeg 获取时长也失败: {e}")

    return None


def extract_segment_to_wav(input_file, output_wav, start_ms, end_ms):
    """
    使用 ffmpeg 從源檔案中直接擷取一段音訊並儲存為單聲道 16-bit WAV。
    利用 input-seeking (-ss 在 -i 之前) 實現高速定位，不載入整個檔案。
    """
    start_sec = start_ms / 1000.0
    duration_sec = (end_ms - start_ms) / 1000.0

    cmd = [
        FFMPEG,
        '-y',
        '-v', 'warning',
        '-ss', f'{start_sec:.3f}',
        '-i', input_file,
        '-t', f'{duration_sec:.3f}',
        '-vn',                     # 丟棄影片流
        '-ac', '1',                # 單聲道
        '-acodec', 'pcm_s16le',   # 16-bit PCM
        '-f', 'wav',
        output_wav
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 提取片段失败:\n{result.stderr[:500]}")

    if not os.path.exists(output_wav) or os.path.getsize(output_wav) < 100:
        raise RuntimeError(f"ffmpeg 输出文件为空或不存在: {output_wav}")


# ==============================================================================
# 音訊處理核心函式
# ==============================================================================

def check_for_processing_artifacts(initial_analysis, final_analysis):
    warnings = []

    initial_clips = initial_analysis['clipping']['clip_count']
    final_clips = final_analysis['clipping']['clip_count']

    if final_clips > 0 and final_clips > initial_clips:
        new_clips = final_clips - initial_clips
        warnings.append(
            f"   [严重] 处理引入了 {new_clips} 个新的削波样本！\n"
            f"      -> 建议: 降低 `COMPRESSOR['makeup_gain_db']` 或 "
            f"`NORMALIZATION['peak_target_dbfs']` 的值，\n"
            f"         或将 `COMPRESSOR['below_threshold_boost_enabled']` 设为 False。"
        )

    final_crest_factor = final_analysis['crest_factor_db']
    if final_crest_factor < 8.0:
        warnings.append(
            f"   [注意] 峰值因数仅为 {final_crest_factor:.2f} dB，可能存在过度压缩。\n"
            f"      -> 建议: 尝试减小 `COMPRESSOR['ratio']` 或 `makeup_gain_db`。"
        )

    if warnings:
        print("\n" + "="*20 + " ⚠️ 自动失真监测警告 ⚠️ " + "="*20)
        for warning in warnings:
            print(warning)
        print("=" * 66)


def separate_vocals_with_audio_separator(wav_file, work_dir):
    """對單個 WAV 片段進行人聲分離"""
    if not AUDIO_SEPARATOR_AVAILABLE:
        print("   ⚠ audio-separator 未安装或无法加载，跳过人声分离")
        return None

    if not PROCESSING_CONFIG["SEPARATION"]["enabled"]:
        return None

    tmp_out = os.path.join(work_dir, "separated")
    os.makedirs(tmp_out, exist_ok=True)

    config = PROCESSING_CONFIG["SEPARATION"]

    try:
        print(f"\n   🎵 正在对片段进行人声分离...")
        print(f"      模型: {config['model_name']}")
        print(f"      GPU加速: {'启用' if config['use_cuda'] else '禁用'}")

        separator = Separator(output_dir=tmp_out)
        separator.output_format = config['output_format']
        separator.use_cuda = config['use_cuda']
        separator.denoise = config['denoise']
        separator.normalize = config['normalize']
        separator.segment_size = config['segment_size']
        separator.overlap = config['overlap']
        separator.model_name = config['model_name']

        print("      正在加载模型...")
        separator.load_model()

        print("      正在分离人声...")
        separator.separate(wav_file)

        vocals_file = None
        for filename in os.listdir(tmp_out):
            filename_lower = filename.lower()
            if 'vocals' in filename_lower or 'vocal' in filename_lower:
                vocals_file = os.path.join(tmp_out, filename)
                break

        if vocals_file and os.path.exists(vocals_file):
            file_size_mb = os.path.getsize(vocals_file) / (1024 * 1024)
            print(f"      ✓ 人声分离成功！({file_size_mb:.2f} MB)")
            return vocals_file
        else:
            print(f"      ⚠ 未找到 vocals 输出文件")
            return None

    except Exception as e:
        print(f"      ❌ 人声分离失败: {str(e)}")
        return None


def apply_highpass_lowpass_filters(audio, highpass_enabled, lowpass_enabled,
                                   highpass_cutoff, lowpass_cutoff, order):
    if not highpass_enabled and not lowpass_enabled:
        print("   ⓘ 高通和低通滤波均已禁用，跳过此步骤。")
        return audio

    print(f"   正在应用滤波器...")
    print(f"      - 高通滤波: {highpass_cutoff} Hz" if highpass_enabled
          else "      - 高通滤波: [已禁用]")
    print(f"      - 低通滤波: {lowpass_cutoff} Hz" if lowpass_enabled
          else "      - 低通滤波: [已禁用]")

    samples = np.array(audio.get_array_of_samples()).astype(np.float32)
    sample_rate = audio.frame_rate
    max_val = float(2 ** (audio.sample_width * 8 - 1))
    samples /= max_val

    nyquist = sample_rate / 2.0
    filtered_samples = samples

    if highpass_enabled and highpass_cutoff > 0:
        high_normal = highpass_cutoff / nyquist
        if high_normal < 1.0:
            sos_high = signal.butter(order, high_normal, btype='high', output='sos')
            filtered_samples = signal.sosfiltfilt(sos_high, filtered_samples)
        else:
            print(f"      ⚠ 高通截止频率高于奈奎斯特频率，已跳过。")

    if lowpass_enabled and lowpass_cutoff > 0:
        low_normal = lowpass_cutoff / nyquist
        if low_normal < 1.0:
            sos_low = signal.butter(order, low_normal, btype='low', output='sos')
            filtered_samples = signal.sosfiltfilt(sos_low, filtered_samples)
        else:
            print(f"      ⚠ 低通截止频率高于奈奎斯特频率，已跳过。")

    filtered_samples = np.clip(filtered_samples, -1.0, 1.0)

    int_type = np.int16 if audio.sample_width == 2 else np.int32
    filtered_samples = (filtered_samples * max_val).astype(int_type)
    filtered_audio = audio._spawn(filtered_samples.tobytes())

    original_rms = audio.dBFS
    filtered_rms = filtered_audio.dBFS
    rms_change = filtered_rms - original_rms

    print(f"   ✓ 滤波完成，RMS变化: {rms_change:+.2f} dB")
    return filtered_audio


def compress_audio(audio, enabled, threshold_db, ratio, makeup_gain_db,
                   below_threshold_boost_enabled=True,
                   below_threshold_boost_ratio=0.3):
    """動態壓縮 + 可選的閾值以下小訊號增強"""
    if not enabled:
        print("   ⓘ 动态压缩已禁用，跳过此步骤。")
        return audio

    print(f"   正在应用动态压缩...")
    print(f"      - 阈值: {threshold_db} dB, 比率: {ratio}:1, 补偿增益: {makeup_gain_db} dB")

    # ✨ 輸出小訊號增強開關狀態
    if below_threshold_boost_enabled:
        print(f"      - 小信号增强: ✓ 已启用 "
              f"(最多 +{int(below_threshold_boost_ratio * 100)}% 渐进提升)")
    else:
        print(f"      - 小信号增强: ✗ 已禁用 (阈值以下信号原样通过)")

    samples = np.array(audio.get_array_of_samples()).astype(np.float32)
    max_val = float(2 ** (audio.sample_width * 8 - 1))
    samples /= max_val

    threshold_linear = 10 ** (threshold_db / 20.0)
    abs_samples = np.abs(samples)
    signs = np.sign(samples)

    above_threshold = abs_samples > threshold_linear
    below_threshold = ~above_threshold

    compressed = np.zeros_like(samples)

    # 閾值以上：按比率壓縮
    if np.any(above_threshold):
        compressed_abs = threshold_linear + (abs_samples[above_threshold] - threshold_linear) / ratio
        compressed[above_threshold] = signs[above_threshold] * compressed_abs

    # ✨ 閾值以下：受開關控制
    if below_threshold_boost_enabled:
        # 漸進提升：訊號越小，提升越多（最多 +ratio*100%）
        if np.any(below_threshold):
            boost_factor = 1.0 + (1.0 - abs_samples[below_threshold] / threshold_linear) * below_threshold_boost_ratio
            compressed[below_threshold] = samples[below_threshold] * boost_factor
    else:
        # 開關關閉：閾值以下訊號保持原樣，不做任何提升
        if np.any(below_threshold):
            compressed[below_threshold] = samples[below_threshold]

    if makeup_gain_db != 0:
        gain_linear = 10 ** (makeup_gain_db / 20.0)
        compressed *= gain_linear

    compressed = np.clip(compressed, -1.0, 1.0)

    int_type = np.int16 if audio.sample_width == 2 else np.int32
    compressed = (compressed * max_val).astype(int_type)
    return audio._spawn(compressed.tobytes())


def detect_clipping(audio, threshold=0.999):
    samples = np.array(audio.get_array_of_samples())
    if samples.size == 0:
        return {'has_clipping': False, 'clip_count': 0,
                'clip_ratio': 0.0, 'clip_percentage': 0.0}

    max_val = float(2 ** (audio.sample_width * 8 - 1))
    normalized_samples = samples.astype(np.float32) / max_val

    clipped = np.abs(normalized_samples) >= threshold
    clip_count = np.sum(clipped)
    total_samples = len(samples)
    clip_ratio = clip_count / total_samples if total_samples > 0 else 0

    return {
        'has_clipping': clip_count > 0,
        'clip_count': int(clip_count),
        'clip_ratio': clip_ratio,
        'clip_percentage': clip_ratio * 100
    }


def analyze_distortion(audio):
    samples = np.array(audio.get_array_of_samples()).astype(np.float32)
    if samples.size == 0:
        return {
            'clipping': {'has_clipping': False, 'clip_count': 0, 'clip_percentage': 0.0},
            'crest_factor_db': 0,
            'dynamic_range_db': 0,
            'peak_dbfs': -np.inf,
            'rms_dbfs': -np.inf
        }

    max_val = float(2 ** (audio.sample_width * 8 - 1))
    normalized_samples = samples / max_val

    clip_info = detect_clipping(audio)

    rms = np.sqrt(np.mean(normalized_samples ** 2))
    peak = np.max(np.abs(normalized_samples)) if normalized_samples.size > 0 else 0
    crest_factor = peak / rms if rms > 1e-9 else 0
    crest_factor_db = 20 * np.log10(crest_factor) if crest_factor > 1e-9 else -np.inf

    dynamic_range_db = audio.max_dBFS - audio.dBFS if audio.dBFS > -np.inf else 0

    return {
        'clipping': clip_info,
        'crest_factor_db': crest_factor_db,
        'dynamic_range_db': dynamic_range_db,
        'peak_dbfs': audio.max_dBFS,
        'rms_dbfs': audio.dBFS,
    }


def print_distortion_report(distortion_info, stage_name=""):
    prefix = f" [{stage_name}] " if stage_name else " "
    print(f"{prefix}音频质量分析:")
    print(f"{prefix}   - 峰值: {distortion_info['peak_dbfs']:.2f} dBFS")
    print(f"{prefix}   - 平均(RMS): {distortion_info['rms_dbfs']:.2f} dBFS")

    clip = distortion_info['clipping']
    if clip['has_clipping']:
        print(f"{prefix}   - 削波: ⚠ 检测到 {clip['clip_count']} 个样本 "
              f"({clip['clip_percentage']:.3f}%)")
    else:
        print(f"{prefix}   - 削波: ✓ 无明显削波失真")

    cf = distortion_info['crest_factor_db']
    cf_desc = " (正常)"
    if cf != -np.inf:
        if cf < 8:
            cf_desc = " (可能过度压缩)"
        elif cf > 18:
            cf_desc = " (动态范围过大)"
    print(f"{prefix}   - 峰值因数: {cf:.2f} dB{cf_desc}")

    dr = distortion_info['dynamic_range_db']
    dr_desc = " (适中)"
    if dr < 8:
        dr_desc = " (较小)"
    elif dr > 18:
        dr_desc = " (较大)"
    print(f"{prefix}   - 动态范围: {dr:.2f} dB{dr_desc}")


def peak_normalize(audio, enabled, peak_target_dbfs):
    if not enabled:
        print("   ⓘ 峰值标准化已禁用，跳过此步骤。")
        return audio

    change_in_dBFS = peak_target_dbfs - audio.max_dBFS

    if change_in_dBFS > 0.01:
        print(f"   正在应用峰值标准化 (目标: {peak_target_dbfs} dBFS)，"
              f"增益: {change_in_dBFS:+.2f} dB")
        return audio.apply_gain(change_in_dBFS)

    print(f"   峰值已接近 {peak_target_dbfs} dBFS，无需标准化。")
    return audio


def get_audio_files(directory):
    print(f"\n正在搜索目录: {directory}")
    audio_files = []
    supported_exts = ['.mp3', '.wav', '.mp4', '.flac']

    try:
        all_items = os.listdir(directory)
        for item in all_items:
            item_path = os.path.join(directory, item)
            if os.path.isfile(item_path):
                ext = os.path.splitext(item)[1].lower()
                if ext in supported_exts:
                    audio_files.append(item_path)

        print(f"找到 {len(audio_files)} 个支持的文件: "
              f"{', '.join([os.path.basename(f) for f in audio_files[:5]])}"
              f"{'...' if len(audio_files) > 5 else ''}")
    except Exception as e:
        print(f"读取目录时出错: {str(e)}")

    return audio_files


def create_empty_srt(audio_file):
    srt_file = os.path.splitext(audio_file)[0] + ".srt"
    try:
        with open(srt_file, 'w', encoding='utf-8') as f:
            pass
    except Exception as e:
        print(f" ❌ 创建SRT失败: {e}")


# ==============================================================================
# ✨ 核心處理流程：先分割 → 再逐段處理 ✨
# ==============================================================================

def convert_and_split_audio(input_file, output_dir):
    print(f"\n{'='*66}")
    print(f"正在处理: {os.path.basename(input_file)}")
    print(f"{'='*66}")

    temp_dir = tempfile.mkdtemp(prefix="audio_proc_")

    try:
        # ── 準備：用 ffprobe 取得時長（不載入檔案）───────────
        print(f"\n [准备] 正在获取音频时长（不加载文件到内存）...")
        duration_ms = get_audio_duration_ms(input_file)
        if duration_ms is None or duration_ms <= 0:
            raise RuntimeError(
                "无法获取音频时长。请确保 ffprobe 或 ffmpeg 在系统 PATH 中。"
            )

        duration_sec = duration_ms / 1000.0
        duration_min = duration_sec / 60.0
        print(f" ✓ 音频时长: {duration_sec:.1f} 秒 ({duration_min:.1f} 分钟)")

        base_name = Path(input_file).stem
        output_config = PROCESSING_CONFIG["OUTPUT"]
        split_threshold_ms = output_config["split_duration_min"] * 60 * 1000
        overlap_ms = output_config["split_overlap_sec"] * 1000
        enable_splitting = output_config.get("enable_splitting", True)

        # ✨ 輸出格式相關（FLAC 無損）
        output_format = output_config.get("output_format", "flac").lower()
        output_ext = f".{output_format}"
        flac_compression_level = output_config.get("flac_compression_level", 5)

        should_split = enable_splitting and duration_ms > split_threshold_ms

        # ── 步驟 1：計算分割方案 ─────────────────────────
        segments = []

        if not should_split:
            # 不需要分割 → 當作單個片段
            if duration_ms > split_threshold_ms:
                print(f"\n ⓘ 文件时长超过 {output_config['split_duration_min']} 分钟，"
                      f"但分割功能已禁用，将处理为单个文件。")

            segments.append({
                'index': 0,
                'actual_start_ms': 0,
                'actual_end_ms': duration_ms,
                'logical_chunk_duration_ms': duration_ms,
                'output_file': os.path.join(output_dir, f"{base_name}{output_ext}"),
                'label': '单文件',
            })
        else:
            num_parts = (duration_ms + split_threshold_ms - 1) // split_threshold_ms
            avg_duration_ms = duration_ms / num_parts

            print(f"\n ✂️ 智能分割: 共 {num_parts} 个部分")
            print(f"   每段约 {avg_duration_ms/1000:.1f} 秒, "
                  f"{output_config['split_overlap_sec']} 秒向前重叠")

            for i in range(num_parts):
                logical_start_ms = i * avg_duration_ms
                logical_end_ms = (
                    (i + 1) * avg_duration_ms
                    if i < num_parts - 1
                    else duration_ms
                )
                # 向前重疊
                actual_start_ms = max(0, logical_start_ms - overlap_ms)
                actual_end_ms = logical_end_ms
                logical_chunk_duration_ms = int(logical_end_ms - logical_start_ms)
                output_filename_base = (
                    f"{base_name}_part{i+1:02d}_{logical_chunk_duration_ms}ms"
                )
                output_file = os.path.join(
                    output_dir, f"{output_filename_base}{output_ext}")

                segments.append({
                    'index': i,
                    'actual_start_ms': int(actual_start_ms),
                    'actual_end_ms': int(actual_end_ms),
                    'logical_chunk_duration_ms': logical_chunk_duration_ms,
                    'output_file': output_file,
                    'label': f'Part {i+1}/{num_parts}',
                })

        # ── 步驟 2-6：逐片段處理 ─────────────────────────
        total_segments = len(segments)

        for seg in segments:
            idx = seg['index']
            label = seg['label']

            print(f"\n   {'─'*55}")
            print(f"   📎 {label} (共 {total_segments} 段)")
            print(f"   {'─'*55}")

            # ── 2. 用 ffmpeg 擷取片段為 WAV ──────────────
            seg_wav = os.path.join(temp_dir, f"seg_{idx:04d}.wav")
            actual_dur_sec = (
                (seg['actual_end_ms'] - seg['actual_start_ms']) / 1000.0
            )

            print(f"\n [步骤 1/5] ⚙️ 提取片段为 WAV（确保兼容性）")
            print(f"      时间范围: {seg['actual_start_ms']/1000:.1f}s → "
                  f"{seg['actual_end_ms']/1000:.1f}s (实际 {actual_dur_sec:.1f}s)")

            extract_segment_to_wav(
                input_file, seg_wav,
                seg['actual_start_ms'], seg['actual_end_ms']
            )
            wav_size_mb = os.path.getsize(seg_wav) / (1024 * 1024)
            print(f"   ✓ WAV 片段已提取 ({wav_size_mb:.1f} MB)")

            # ── 可選：人聲分離 ───────────────────────────
            audio_to_load = seg_wav
            if (PROCESSING_CONFIG["SEPARATION"]["enabled"]
                    and AUDIO_SEPARATOR_AVAILABLE):
                vocals = separate_vocals_with_audio_separator(seg_wav, temp_dir)
                if vocals:
                    audio_to_load = vocals
                    print(f"   ✓ 将使用分离后的人声文件继续处理")
                else:
                    print(f"   ⓘ 人声分离未成功，使用原始片段继续")

            # ── 載入 WAV 到 pydub ────────────────────────
            audio = AudioSegment.from_file(audio_to_load)
            audio = audio.set_channels(1)
            print(f"   ✓ 加载成功 (时长: {len(audio)/1000:.1f}s, "
                  f"采样率: {audio.frame_rate}Hz)")

            # ── 處理前分析 ───────────────────────────────
            initial_analysis = analyze_distortion(audio)
            print_distortion_report(initial_analysis, "处理前")

            # ── 3. 濾波去噪 ─────────────────────────────
            print(f"\n [步骤 2/5] 🔊 滤波去噪")
            audio = apply_highpass_lowpass_filters(
                audio, **PROCESSING_CONFIG["FILTER"]
            )

            # ── 4. 動態壓縮 ─────────────────────────────
            print(f"\n [步骤 3/5] 📊 动态压缩")
            audio = compress_audio(audio, **PROCESSING_CONFIG["COMPRESSOR"])

            # ── 5. 峰值正規化 ────────────────────────────
            print(f"\n [步骤 4/5] 📈 峰值标准化")
            audio = peak_normalize(audio, **PROCESSING_CONFIG["NORMALIZATION"])

            # ── 6. 自動失真監測 ──────────────────────────
            print(f"\n [步骤 5/5] 🔍 自动失真监测")
            final_analysis = analyze_distortion(audio)
            print_distortion_report(final_analysis, "处理后")

            rms_boost = final_analysis['rms_dbfs'] - initial_analysis['rms_dbfs']
            print(f"   [总结] 平均音量提升: {rms_boost:+.2f} dB")

            check_for_processing_artifacts(initial_analysis, final_analysis)

            # ── 匯出 FLAC（無損）─────────────────────────
            print(f"\n [导出] 正在导出 {output_format.upper()} "
                  f"(压缩等级 {flac_compression_level}, 无损)...")
            if output_format == "flac":
                audio.export(
                    seg['output_file'],
                    format="flac",
                    parameters=["-compression_level", str(flac_compression_level)]
                )
            else:
                audio.export(seg['output_file'], format=output_format)

            out_size_mb = os.path.getsize(seg['output_file']) / (1024 * 1024)
            print(f"\n ✓ 已导出: {os.path.basename(seg['output_file'])} "
                  f"({out_size_mb:.1f} MB)")

            create_empty_srt(seg['output_file'])

            # ── 清理本片段臨時檔案（釋放磁碟空間）────────
            if os.path.exists(seg_wav):
                try:
                    os.remove(seg_wav)
                except Exception:
                    pass

            sep_dir = os.path.join(temp_dir, "separated")
            if os.path.exists(sep_dir):
                shutil.rmtree(sep_dir, ignore_errors=True)

    finally:
        # 清理整個臨時目錄
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


# ==============================================================================
# ✨ 主函式 ✨
# ==============================================================================

def main():
    if getattr(sys, 'frozen', False):
        script_dir = os.path.dirname(sys.executable)
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))

    print("\n" + "=" * 66)
    print("🎵 音频处理脚本 V9.0 - FLAC 无损输出版")
    print("=" * 66)

    print(f"\n📋 环境检查:")
    print(f"   - Python: {sys.version.split()[0]}")
    print(f"   - ffmpeg: {FFMPEG}")
    print(f"   - ffprobe: {FFPROBE}")
    print(f"   - Audio-Separator: "
          f"{'✓ 已安装' if AUDIO_SEPARATOR_AVAILABLE else '✗ 未安装或加载失败'}")

    if AUDIO_SEPARATOR_AVAILABLE:
        sep_enabled = PROCESSING_CONFIG['SEPARATION']['enabled']
        print(f"   - 人声分离: {'✓ 已启用' if sep_enabled else '✗ 已禁用'}")
        if sep_enabled:
            print(f"     - 模型: {PROCESSING_CONFIG['SEPARATION']['model_name']}")
            print(f"     - GPU加速: "
                  f"{'✓ 启用' if PROCESSING_CONFIG['SEPARATION']['use_cuda'] else '✗ 禁用'}")

    output_dir = os.path.join(script_dir, "converted_audio")
    os.makedirs(output_dir, exist_ok=True)
    print(f"\n📁 输出目录: {output_dir}")

    audio_files = get_audio_files(script_dir)

    if not audio_files:
        print("\n" + "=" * 66)
        print("❌ 未在脚本目录中找到任何支持的音频/视频文件。")
        print("   支持格式: .mp3, .wav, .flac, .mp4")
        wait_for_enter()
        return

    print("\n" + "=" * 66)
    print("🔧 处理流程 (先分割 → 再逐段处理，彻底避免 4GB 限制):")

    output_config = PROCESSING_CONFIG['OUTPUT']
    enable_splitting = output_config.get("enable_splitting", True)

    if enable_splitting:
        print(f"   1. ✂️ 智能分割 (>{output_config['split_duration_min']}分钟，"
              f"带{output_config['split_overlap_sec']}秒向前重叠)")
    else:
        print("   1. ✂️ 智能分割 (已禁用)")

    print("   2. ⚙️ 预处理为WAV (确保兼容性)")

    if AUDIO_SEPARATOR_AVAILABLE and PROCESSING_CONFIG['SEPARATION']['enabled']:
        print("   2.5 🎵 MDX23C 人声分离 (GPU加速)")

    filter_cfg = PROCESSING_CONFIG['FILTER']
    hp_status = '✓' if filter_cfg.get('highpass_enabled') else '✗'
    lp_status = '✓' if filter_cfg.get('lowpass_enabled') else '✗'
    print(f"   3. 🔊 滤波去噪 (高通: {hp_status}, 低通: {lp_status})")

    comp_cfg = PROCESSING_CONFIG['COMPRESSOR']
    comp_status = '✓ 已启用' if comp_cfg.get('enabled') else '✗ 已禁用'
    boost_ratio = comp_cfg.get('below_threshold_boost_ratio', 0.3)
    boost_status = ('✓ 已启用' if comp_cfg.get('below_threshold_boost_enabled')
                    else '✗ 已禁用')
    print(f"   4. 📊 动态压缩: {comp_status}")
    print(f"      └─ 小信号增强 (+{int(boost_ratio*100)}% 渐进提升): {boost_status}")

    norm_status = ('✓ 已启用' if PROCESSING_CONFIG['NORMALIZATION'].get('enabled')
                   else '✗ 已禁用')
    print(f"   5. 📈 峰值标准化: {norm_status}")
    print("   6. 🔍 自动失真监测")

    out_fmt = output_config.get("output_format", "flac").upper()
    print(f"   7. 💾 导出 {out_fmt} "
          f"(压缩等级 {output_config.get('flac_compression_level', 5)}, 无损)")
    print("=" * 66)

    print("\n💡 提示: 所有参数可在脚本顶部 PROCESSING_CONFIG 中调整")
    print("💡 核心改进: 先用 ffmpeg 分割再逐段处理，彻底避免 WAV 4GB 限制")
    print("💡 FLAC 说明: 无损格式，压缩等级只影响文件大小/速度，不影响音质")

    input("\n按Enter键开始处理...")

    success_count = 0
    fail_count = 0

    for audio_file in tqdm(audio_files, desc="整体进度", unit="文件"):
        try:
            convert_and_split_audio(audio_file, output_dir)
            success_count += 1
        except Exception as e:
            print(f"\n ❌ 处理文件 {os.path.basename(audio_file)} 时发生严重错误:")
            print(f"    {str(e)}")
            fail_count += 1

    print("\n")
    print("=" * 66)
    print(f"✅ 全部处理完成！")
    print(f"   成功: {success_count} 个")
    print(f"   失败: {fail_count} 个")
    print("=" * 66)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n\n💥 程序发生未捕获的错误: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        # ✨ 統一走開關：WAIT_FOR_ENTER_BEFORE_EXIT 為 False 時直接退出
        wait_for_enter()