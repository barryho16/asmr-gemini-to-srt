# coding: utf-8
import os
import re
import sys
import traceback

# --- 設定 ---
# MP3 切割時的重疊秒數，單位是毫秒。例如 5 秒就設定為 5000
# 這個值必須跟你的 MP3 切割腳本設定的重疊值一致
OVERLAP_MS = 5000

def fix_timestamp_format(line):
    """修正 SRT 時間格式，處理多種格式錯誤"""
    
    if "-->" not in line:
        return line
    
    # 分割開始和結束時間
    parts = line.split(" --> ")
    if len(parts) != 2:
        return line
    
    start_time = parts[0].strip()
    end_time = parts[1].strip()
    
    # 修正單個時間字串
    def fix_single_time(time_str):
        # 已經是正確格式 hh:mm:ss,ms 就直接傳回
        if re.match(r"^\d{2}:\d{2}:\d{2},\d{3}$", time_str):
            return time_str
        
        # 格式 1: mm:ss.ms (00:11.777) -> 00:00:11,777 (點號改逗號，補小時)
        match = re.match(r"^(\d{2}):(\d{2})\.(\d{3})$", time_str)
        if match:
            return f"00:{match.group(1)}:{match.group(2)},{match.group(3)}"
        
        # 格式 2: m:ss.ms (1:11.777) -> 00:01:11,777 (點號改逗號，補小時和分鐘)
        match = re.match(r"^(\d{1}):(\d{2})\.(\d{3})$", time_str)
        if match:
            return f"00:{int(match.group(1)):02}:{match.group(2)},{match.group(3)}"
        
        # 格式 3: mm:ss:ms (09:55:921) -> 00:09:55,921
        match = re.match(r"^(\d{2}):(\d{2}):(\d{3})$", time_str)
        if match:
            return f"00:{match.group(1)}:{match.group(2)},{match.group(3)}"
        
        # 格式 4: m:ss,ms 或 mm:ss,ms (1:12,650) -> 00:01:12,650
        match = re.match(r"^(\d{1,2}):(\d{2}),(\d{3})$", time_str)
        if match:
            return f"00:{int(match.group(1)):02}:{match.group(2)},{match.group(3)}"
        
        # 格式 5: h:mm:ss,ms (1:31:44,500) -> 01:31:44,500
        match = re.match(r"^(\d{1}):(\d{2}):(\d{2}),(\d{3})$", time_str)
        if match:
            return f"0{match.group(1)}:{match.group(2)}:{match.group(3)},{match.group(4)}"
        
        # 格式 6: hh:mm:ss.ms (00:00:11.777) -> 00:00:11,777 (只需把點號改逗號)
        match = re.match(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})$", time_str)
        if match:
            return f"{match.group(1)}:{match.group(2)}:{match.group(3)},{match.group(4)}"
        
        # 無法辨識的格式，傳回原字串
        return time_str
    
    # 修正兩個時間
    start_fixed = fix_single_time(start_time)
    end_fixed = fix_single_time(end_time)
    
    return f"{start_fixed} --> {end_fixed}"

def add_offset_to_time(time_str, offset_ms):
    """將時間字串加上偏移量（毫秒），容錯處理不完整格式"""
    # 支援 [hh:]mm:ss,ms，hh 可省略
    m = re.match(r"(?:(\d{1,2}):)?(\d{2}):(\d{2}),(\d{3})$", time_str)
    if not m:
        # 格式不符，直接回傳原字串避免崩潰
        return time_str
    h = int(m.group(1)) if m.group(1) else 0
    mnt = int(m.group(2))
    s = int(m.group(3))
    ms = int(m.group(4))

    # 將時間轉為毫秒，加上偏移量
    total_ms = (h * 3600 + mnt * 60 + s) * 1000 + ms + offset_ms
    if total_ms < 0:
        total_ms = 0
    
    # 轉回時:分:秒,毫秒格式
    h = total_ms // 3600000
    mnt = (total_ms % 3600000) // 60000
    s = (total_ms % 60000) // 1000
    ms = total_ms % 1000
    return f"{h:02}:{mnt:02}:{s:02},{ms:03}"

def apply_offset(line, offset_ms):
    """將字幕時間軸加上偏移（毫秒）"""
    if "-->" not in line:
        return line
    start, end = line.split(" --> ")
    start_new = add_offset_to_time(start.strip(), offset_ms)
    end_new = add_offset_to_time(end.strip(), offset_ms)
    return f"{start_new} --> {end_new}"

def strip_leading_until_first_sequence(lines):
    """
    移除在第一個序號 '1'（單獨一行）之前的所有文字。
    確保從 '1' 開始保留完整的字幕區塊。
    """
    # 尋找第一個獨立的 '1'
    for i, l in enumerate(lines):
        # 嚴格匹配：只有數字 1 的行（前後可有空白）
        if re.match(r'^\s*1\s*$', l):
            return lines[i:]
    
    # 如果找不到獨立的 '1'，尋找第一個時間戳並嘗試重建
    for i, l in enumerate(lines):
        if "-->" in l:
            # 檢查前一行是否為序號
            if i > 0 and re.match(r'^\s*\d+\s*$', lines[i-1]):
                return lines[i-1:]
            else:
                # 時間戳前沒有序號，從時間戳開始並補上序號 '1'
                return ['1'] + lines[i:]
    
    # 完全找不到有效內容，傳回原始內容
    return lines

def process_srt_file(filepath, offset_ms=0):
    """讀取 SRT，修正格式 + 加上偏移（毫秒）"""
    new_lines = []
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    # 移除在第一個序號 '1' 之前的所有文字
    lines = strip_leading_until_first_sequence(lines)

    for line in lines:
        line = fix_timestamp_format(line)
        line = apply_offset(line.rstrip("\n"), offset_ms)
        new_lines.append(line.strip())
    return new_lines

def extract_duration_from_filename(filename):
    """從檔名中擷取持續時間（毫秒），例如 _450000ms -> 450000"""
    match = re.search(r"_(\d+)ms", filename)
    if match:
        return int(match.group(1))
    return None

def natural_part_sort(file_list):
    """依據 _partXX 的數字做自然排序；沒有 _part 的排在最前"""
    def key_fn(name):
        m = re.search(r"_part(\d+)", name)
        return int(m.group(1)) if m else -1
    return sorted(file_list, key=key_fn)

def ensure_srt_name(name):
    """確保輸出檔名有 .srt 副檔名"""
    return name if name.endswith(".srt") else name + ".srt"

def merge_srt_group(script_dir, file_list, output_name, output_dir):
    """
    合併一組字幕，根據 part 編號、檔名中的平均時長以及設定的重疊時間來動態計算偏移量。
    """
    merged_lines = []
    counter = 1
    file_list = natural_part_sort(file_list)

    # 從第一個檔名中擷取基準平均時長，假設同組檔案的時長標示都相同
    if not file_list:
        return
    avg_duration_ms = extract_duration_from_filename(file_list[0])
    
    # 如果檔名中沒有時長資訊，則無法使用新邏輯，報錯並跳過
    if avg_duration_ms is None:
        print(f"[ERROR] 檔名 {file_list[0]} 中找不到時長資訊 (_XXXms)，無法計算重疊偏移，跳過合併 '{output_name}'")
        return

    for filename in file_list:
        filepath = os.path.join(script_dir, filename)
        
        part_match = re.search(r"_part(\d+)", filename)
        part_num = int(part_match.group(1)) if part_match else 1
        
        # 公式: Offset(Part N) = (N-1) * avg_duration - overlap
        if part_num > 1:
            offset_ms = (part_num - 1) * avg_duration_ms - OVERLAP_MS
        else:
            offset_ms = 0
        
        if offset_ms < 0:
            offset_ms = 0

        print(f"[INFO] 處理 {filename}，計算出的偏移量為: {offset_ms}ms")
        processed_lines = process_srt_file(filepath, offset_ms)

        # 合併字幕內容
        for line in processed_lines:
            if re.match(r"^\d+$", line):
                if merged_lines and merged_lines[-1] != "":
                    merged_lines.append("")
                merged_lines.append(str(counter))
                counter += 1
            elif "-->" in line:
                merged_lines.append(line)
            elif line.strip() == "":
                if merged_lines and merged_lines[-1] != "":
                    merged_lines.append("")
            else:
                merged_lines.append(line)

    if merged_lines and merged_lines[-1] != "":
        merged_lines.append("")

    output_name = ensure_srt_name(output_name)
    output_path = os.path.join(output_dir, output_name)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(merged_lines))

    print(f"[OK] 輸出 {output_path}")


def main():
    # 強制以 .py 所在資料夾為工作目錄（雙擊執行時很重要）
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # 建立輸出資料夾
    output_dir = os.path.join(script_dir, "merged_srt")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"[INFO] 已創建輸出資料夾: {output_dir}")
    else:
        print(f"[INFO] 使用輸出資料夾: {output_dir}")

    # 掃描 .py 同資料夾的 .srt 檔
    files = [f for f in os.listdir(script_dir) if f.endswith(".srt")]
    if not files:
        print("找不到任何 .srt 檔案。請把字幕檔放到此 .py 同資料夾再執行。")
        return

    # 分組：去掉 _partXX 和 _XXXms，合併同一基底名稱
    groups = {}
    for f in files:
        base = re.sub(r"_part\d+", "", f)
        base = re.sub(r"_\d+ms", "", base)
        groups.setdefault(base, []).append(f)

    # --- ✨ 主要修改區域開始 ---
    # 合併處理
    for base, file_list in groups.items():
        # 情況一：如果組內檔案超過一個，執行合併邏輯
        if len(file_list) > 1:
            print(f"\n--- 正在合併檔案組: {base} ---")
            output_name = base  # 最終輸出就是去掉 _partXX 和 _XXXms 的乾淨名稱
            merge_srt_group(script_dir, file_list, output_name, output_dir)
        # 情況二：如果組內只有一個檔案，執行單一檔案修正與複製邏輯
        elif len(file_list) == 1:
            filename = file_list[0]
            print(f"\n--- 正在處理單一檔案: {filename} ---")
            
            # 1. 讀取並修復檔案內容 (偏移量為 0)
            filepath = os.path.join(script_dir, filename)
            print(f"[INFO] 正在對 {filename} 進行格式修正...")
            fixed_lines = process_srt_file(filepath, offset_ms=0)
            
            # 2. 將修復後的內容寫入到輸出資料夾
            output_path = os.path.join(output_dir, filename)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("\n".join(fixed_lines))
            
            print(f"[OK] 已將修正後的檔案輸出至 {output_path}")
    # --- ✨ 主要修改區域結束 ---

if __name__ == "__main__":
    try:
        main()
        print("\n處理完成！")
    except Exception:
        print("\n程序發生錯誤：")
        traceback.print_exc()