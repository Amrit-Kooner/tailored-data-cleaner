# audit.py
"""
The audit log: an ordered list of {"step", "detail"} entries per file.
"""

import os


def new_log(path):
    return {"source_file": os.path.basename(path), "steps": []}


def log_step(log, name, detail):
    """
    Adds one entry to the audit log for a single cleaning step. Writes to the
    log dict only -- no console print, since the pipeline runs unattended in
    the background and nobody's watching the console per-step. The saved log
    file (written by output/report.py) is where this detail actually gets read.
    """
    log["steps"].append({"step": name, "detail": detail})
