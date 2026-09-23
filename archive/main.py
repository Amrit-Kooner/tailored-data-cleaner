"""
The entry point -- what you actually run (via run-clean.bat). Scans the
drop folder once, cleans every file it finds, saves each result to
Downloads, then exits. Does NOT wait for a specific file and does NOT
loop forever.
"""

import os
import glob

from config import WATCH_DIR, OUTPUT_ROOT, EXTENSIONS
from pipeline import clean_file
from excel_output import save_as_formatted_excel


def run_batch():
    os.makedirs(WATCH_DIR, exist_ok=True)

    files = []
    for ext in EXTENSIONS:
        files.extend(glob.glob(os.path.join(WATCH_DIR, f"*.{ext}")))

    if not files:
        print("No files found in to_clean.")
        return

    results = []
    for path in files:
        filename = os.path.basename(path)
        print(f"Processing: {filename}...")

        try:
            df, original_df, log, dataset_summary, date_cols = clean_file(path)
        except Exception as e:
            print(f"Failed: {filename} -- {e}")
            results.append((filename, False))
            continue

        base_name = os.path.splitext(filename)[0]
        target_dir = os.path.join(OUTPUT_ROOT, f"{base_name} - CLEANED")
        os.makedirs(target_dir, exist_ok=True)

        xlsx_out = os.path.join(target_dir, f"{base_name}_cleaned.xlsx")
        save_as_formatted_excel(df, xlsx_out, date_cols)

        log_path = os.path.join(target_dir, "final_file_log.txt")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Final dataset summary for {filename}\n")
            f.write("=" * 40 + "\n")
            f.write(dataset_summary + "\n")
            f.write("=" * 40 + "\n")
            f.write(f"Audit log ({len(log['steps'])} steps):\n")
            for idx, entry in enumerate(log["steps"], start=1):
                f.write(f"{idx}. {entry['step']}: {entry['detail']}\n")

        try:
            os.remove(path)
        except PermissionError:
            print(f"Warning: could not remove {filename} from to_clean (file may be open elsewhere)")

        print(f"Success: {filename} -> {target_dir}")
        results.append((filename, True))

    succeeded = sum(1 for _, ok in results if ok)
    print(f"Done: {succeeded}/{len(results)} file(s) cleaned successfully.")


if __name__ == "__main__":
    run_batch()
