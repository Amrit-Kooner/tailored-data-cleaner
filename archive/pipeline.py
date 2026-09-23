"""
The conductor. Doesn't do any cleaning itself -- just calls everything from
utils.py, dtype_detection.py, and cleaning.py in the correct order for one
file, start to finish, and hands back the result. main.py calls this once
per file in the batch loop.
"""

import os
import re
import pandas as pd

from config import DISGUISED_BLANKS, CATEGORY_MAPPINGS
from utils import log_step, print_details
from cleaning import find_header_row, standardize_casing, apply_category_mapping, resolve_duplicate_columns, fix_encoding_issues
from dtype_detection import detect_date_columns, convert_to_uk_format, infer_and_fix_dtypes


def clean_file(path):
    """
    Runs the full cleaning pipeline on a single file and returns
    (df, original_df, log, dataset_summary, date_cols).

    Every file can look completely different -- different columns, different
    messiness -- so every step here is designed to detect what it's looking
    at rather than assume a fixed schema.
    """
    log = {"source_file": os.path.basename(path), "steps": []}

    # 1. Raw read (to find the header row), then a proper typed read
    if path.lower().endswith((".xlsx", ".xls")):
        raw_df = pd.read_excel(path, header=None)
    else:
        raw_df = pd.read_csv(path, header=None)

    header_row = find_header_row(raw_df)
    if path.lower().endswith((".xlsx", ".xls")):
        original_df = pd.read_excel(path, header=header_row)
    else:
        original_df = pd.read_csv(path, header=header_row)
    df = original_df.copy()
    log_step(log, "find_header_row", f"header found at row {header_row} ({header_row} junk row(s) skipped above it)")

    # 2. Check duplicate column names (flag only)
    renamed = [col for col in df.columns if re.search(r"\.\d+$", str(col))]
    log_step(log, "check_duplicate_fields", f"{len(renamed)} duplicate column name(s) flagged: {renamed}")

    # 3. Check unnamed columns (flag only)
    unnamed_cols = [col for col in df.columns if pd.isna(col) or str(col).startswith("Unnamed")]
    log_step(log, "check_unnamed_columns", f"{len(unnamed_cols)} unnamed column(s) flagged: {unnamed_cols}")

    # 4. Clean whitespace, header names, add source_file
    df["source_file"] = os.path.basename(path)
    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")
    for col in df.select_dtypes(include=["object", "str"]).columns:
        df[col] = df[col].str.strip()
    log_step(log, "clean_whitespace_and_headers", "added source_file column, standardized header names, trimmed whitespace")

    # 5. Fix encoding / special character issues
    df = fix_encoding_issues(df, log)

    # 6. Convert disguised blanks to real NaN, flag missing data
    df = df.replace(DISGUISED_BLANKS, pd.NA)
    missing_count = df.isna().sum(axis=1)
    rows_with_missing = int((missing_count > 0).sum())
    log_step(log, "missing_value_detection", f"{rows_with_missing} row(s) flagged with at least one missing field")

    # 7. Standardize casing (before dedup, so case-variant duplicates get caught)
    df = standardize_casing(df, log)

    # 8. Category value mapping ("USA" -> "United States" etc)
    df = apply_category_mapping(df, CATEGORY_MAPPINGS, log)

    # 9. Resolve duplicate-named columns (e.g. "email"/"email.1")
    df = resolve_duplicate_columns(df, log)

    # 10. Drop duplicate rows
    rows_before = len(df)
    df = df.drop_duplicates()
    log_step(log, "drop_duplicate_rows", f"{rows_before - len(df)} duplicate row(s) removed")

    # 11. Drop blank rows/columns
    rows_before, cols_before = df.shape
    df = df.dropna(how="all").reset_index(drop=True)
    df = df.dropna(axis=1, how="all")
    log_step(log, "drop_blank_rows_and_columns",
             f"{rows_before - df.shape[0]} blank row(s), {cols_before - df.shape[1]} blank column(s) removed")

    # 12. Format dates to UK dd/mm/yy
    date_cols = detect_date_columns(df)
    df = convert_to_uk_format(df, date_cols, log)
    log_step(log, "format_dates", f"{len(date_cols)} date column(s) converted to dd/mm/yy: {date_cols}")

    # 13. Fix data types -- runs last, once everything is genuinely clean
    df = infer_and_fix_dtypes(df, log)

    # 14. Missing values in numeric (price) columns filled with 0
    numeric_cols = df.select_dtypes(include=["number"]).columns
    for col in numeric_cols:
        missing = df[col].isna().sum()
        if missing > 0:
            df[col] = df[col].fillna(0)
            log_step(log, "fill_missing_numeric", f"{col}: {missing} missing value(s) filled with 0")

    # 15. Compare original vs final
    rows_diff = df.shape[0] - original_df.shape[0]
    cols_diff = df.shape[1] - original_df.shape[1]
    original_cols = set(str(c).strip().lower().replace(" ", "_") for c in original_df.columns)
    final_cols = set(df.columns)
    cols_added = final_cols - original_cols
    cols_removed = original_cols - final_cols
    log_step(log, "compare_original_to_final",
             f"original: {original_df.shape[0]} rows / {original_df.shape[1]} cols | "
             f"final: {df.shape[0]} rows / {df.shape[1]} cols | "
             f"row change: {rows_diff:+d}, column change: {cols_diff:+d} | "
             f"columns added: {sorted(cols_added) if cols_added else 'none'} | "
             f"columns removed: {sorted(cols_removed) if cols_removed else 'none'}")

    dataset_summary = print_details(df)
    return df, original_df, log, dataset_summary, date_cols
