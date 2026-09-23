"""
Small, general-purpose helpers with no dependency on any other module in
this project (other than pandas/dateutil). Anything used by more than one
of the other modules, but too small to deserve its own file, lives here.
"""

import pandas as pd
from dateutil import parser as dateparser


def log_step(log, name, detail):
    """
    Adds one entry to the audit log for a single cleaning step. Writes to the
    log dict only -- no console print, since the pipeline runs unattended in
    the background and nobody's watching the console per-step. The saved log
    file (written by main.py) is where this detail actually gets read.
    """
    log["steps"].append({"step": name, "detail": detail})


def print_details(df):
    """
    Builds a plain-text summary of the dataset (shape, dupe count, missing
    values, dtypes). Returns the text rather than printing it, so main.py
    can write it into the saved audit log file.
    """
    missing = df.isna().sum()
    missing = missing[missing > 0]
    lines = [
        f"Rows: {df.shape[0]}",
        f"Columns: {df.shape[1]}",
        "--------------------------------",
        f"Num of Dupe Rows: {df.duplicated().sum()}",
        "--------------------------------",
        f"Missing values by column:\n{missing}" if not missing.empty else "No missing values.",
        "--------------------------------",
        str(df.dtypes),
        "--------------------------------",
    ]
    return "\n".join(lines)


def try_parse_date(value):
    """
    Tries to parse a single value as a date. Returns a datetime, or None if
    it can't be parsed (blank, empty string, or a genuinely invalid date like
    "2023-13-40"). Used as the slow fallback path in dtype_detection.py's
    date functions -- calling this per-cell across a whole column is the
    slowest part of the pipeline, so it's only reached when the fast
    vectorized pass isn't confident enough.
    """
    if pd.isna(value) or str(value).strip() == "":
        return None
    try:
        return dateparser.parse(str(value), dayfirst=False, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None
