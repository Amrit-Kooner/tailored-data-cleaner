# files.py
"""
Finding input files and choosing output folders. Pure filesystem helpers.
"""

import glob
import os


def find_input_files(watch_dir, extensions):
    """Every file in the drop folder whose extension is in `extensions`."""
    files = []
    for ext in extensions:
        files.extend(glob.glob(os.path.join(watch_dir, f"*.{ext}")))
    return files


def get_unique_dir(target_dir):
    """
    Returns target_dir if it doesn't exist yet, otherwise the same path with
    " (1)", " (2)", etc. appended until a free one is found -- same
    convention browsers use for repeat downloads. So cleaning the same
    source file twice produces "name - CLEANED" and "name - CLEANED (1)"
    side by side in Downloads, instead of the second run silently
    overwriting the first run's Excel file and audit log.
    """
    if not os.path.exists(target_dir):
        return target_dir
    n = 1
    while True:
        candidate = f"{target_dir} ({n})"
        if not os.path.exists(candidate):
            return candidate
        n += 1
