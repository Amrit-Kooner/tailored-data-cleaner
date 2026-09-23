# batch.py
"""
The drop-folder loop: scan the folder once, clean every file, save each
result to Downloads, then stop. One bad file never stops the batch.
"""

import os

from config import WATCH_DIR, OUTPUT_ROOT, EXTENSIONS
from .files import find_input_files, get_unique_dir
from .output.excel import save_as_formatted_excel
from .pipeline import clean_file


def save_outputs(result, source_path):
    """
    Writes one workbook -- "<name>_cleaned.xlsx" -- into a fresh output
    folder and returns that folder. The workbook holds every sheet: Cleaned,
    Report, Failed Values, Missing Values, Review, Disguised Blanks, Raw.
    Nothing else is written next to it any more (no separate .txt log).
    """
    filename = os.path.basename(source_path)
    base_name = os.path.splitext(filename)[0]

    # Cleaning the same source file more than once used to land in the same
    # "name - CLEANED" folder every time, so a second run silently overwrote
    # the first run's Excel file and audit log. Now the second run gets
    # "name - CLEANED (1)", the third "name - CLEANED (2)", etc. -- nothing
    # already in Downloads is ever overwritten.
    target_dir = get_unique_dir(os.path.join(OUTPUT_ROOT, f"{base_name} - CLEANED"))
    os.makedirs(target_dir)

    save_as_formatted_excel(result, os.path.join(target_dir, f"{base_name}_cleaned.xlsx"), filename)
    return target_dir


def run_batch():
    os.makedirs(WATCH_DIR, exist_ok=True)

    files = find_input_files(WATCH_DIR, EXTENSIONS)
    if not files:
        print("No files found in to_clean.")
        return

    results = []
    for path in files:
        filename = os.path.basename(path)
        print(f"Processing: {filename}...")

        try:
            result = clean_file(path)
        except Exception as e:
            print(f"Failed: {filename} -- {e}")
            results.append((filename, False))
            continue

        target_dir = save_outputs(result, path)

        try:
            os.remove(path)
        except PermissionError:
            print(f"Warning: could not remove {filename} from to_clean (file may be open elsewhere)")

        print(f"Success: {filename} -> {target_dir}")
        results.append((filename, True))

    succeeded = sum(1 for _, ok in results if ok)
    print(f"Done: {succeeded}/{len(results)} file(s) cleaned successfully.")