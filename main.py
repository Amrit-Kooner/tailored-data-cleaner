"""
The entry point -- what you actually run (via run-clean.bat). Scans the
drop folder once, cleans every file it finds, saves each result to
Downloads, then exits. Does NOT wait for a specific file and does NOT
loop forever.

All the real work lives in the uni_cleaner package; all the settings live
in config.py.
"""

from uni_cleaner.batch import run_batch


if __name__ == "__main__":
    run_batch()
