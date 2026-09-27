import os
import subprocess
import sys
from pathlib import Path


def find_all_py_files(root_dir):
    """遞迴查找所有.py檔案"""
    py_files = []
    for root, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith('.py'):
                full_path = os.path.join(root, file)
                py_files.append(full_path)
    return py_files


def run_py_files_in_order(target_files):
    """按順序運行指定的.py檔案"""
    print(f"\n開始按順序運行 {len(target_files)} 個 Python 文件...\n")
    print("=" * 60)

    for i, file_path in enumerate(target_files, 1):
        file_path = Path(file_path).resolve()

        if not file_path.exists():
            print(f"\n[{i}/{len(target_files)}] 錯誤: 文件不存在 - {file_path}")
            continue

        file_dir = file_path.parent
        file_name = file_path.name

        print(f"\n[{i}/{len(target_files)}] 運行: {file_path}")
        print(f"工作資料夾: {file_dir}")
        print("-" * 60)

        try:
            # 強制子行程使用 UTF-8 編碼
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUTF8'] = '1'

            process = subprocess.Popen(
                [sys.executable, '-u', file_name],
                cwd=str(file_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=0,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace',
                env=env
            )

            for line in iter(process.stdout.readline, ''):
                if line:
                    print(line, end='', flush=True)

            return_code = process.wait()

            if return_code != 0:
                print(f"\n⚠️ 警告: {file_name} 返回碼為 {return_code}")
            else:
                print(f"\n✓ {file_name} 運行成功")

        except Exception as e:
            print(f"❌ 錯誤: 運行 {file_name} 時發生異常: {str(e)}")

        print("-" * 60)

    print("\n" + "=" * 60)
    print("所有腳本運行完成！")


def main():
    # 讓主腳本自己的 stdout 也用 UTF-8
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

    # ============================================================
    # 關鍵修改：
    # 找到目前這個 .py 檔案所在資料夾，並切換到該資料夾
    # ============================================================
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)

    print(f"目前主程式位置: {Path(__file__).resolve()}")
    print(f"已切換工作資料夾到: {script_dir}")
    print("=" * 60)

    root_directory = script_dir

    print("正在掃描目錄...")
    all_py_files = find_all_py_files(root_directory)

    current_script = Path(__file__).resolve()
    all_py_files = [
        f for f in all_py_files
        if Path(f).resolve() != current_script
    ]

    print(f"\n識別了 {len(all_py_files)} 個 .py 文件")
    print("=" * 60)
    for py_file in all_py_files:
        print(f"  - {py_file}")
    print("=" * 60)

    # 這些路徑現在都會以「這個主 .py 所在資料夾」為基準
    target_files = [
        script_dir / "audio_converter_splitter-ai-v3-flac.py",
        script_dir / "converted_audio" / "gemini-3.1-lite-to-srt.py",
        script_dir / "converted_audio" / "copy_references.py",
        script_dir / "converted_audio" / "merge_srt_part-Backward-only-Overlap.py",
        script_dir / "converted_audio" / "merged_srt" / "nv_glm5.1_srt_to_zh_v3.py",

    ]

    run_py_files_in_order(target_files)


if __name__ == "__main__":
    try:
        main()
    finally:
        # 雙擊執行時，避免視窗一跑完就關掉
        input("\n按 Enter 鍵結束...")