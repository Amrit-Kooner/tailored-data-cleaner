"""
The "make the values consistent" steps -- casing, canonical category values,
duplicate-named columns, encoding artifacts, and finding the real header row.
Update CASING_RULES/CATEGORY_MAPPINGS in config.py to change behavior here
without touching this file.
"""

import re
import unicodedata

from config import CASING_RULES, DUPLICATE_COLUMN_MATCH_THRESHOLD, HEADER_DETECTION_THRESHOLD
from utils import log_step


def find_header_row(raw_df, thresh=HEADER_DETECTION_THRESHOLD):
    """
    Finds the row index where the real header lives, skipping any blank/junk
    rows above it. A row counts as "the header" once at least `thresh`
    fraction of its cells are filled in.
    """
    for i, row in raw_df.iterrows():
        if row.notna().sum() / len(row) >= thresh:
            return i
    return 0


def standardize_casing(df, log):
    """
    Applies a casing rule (from CASING_RULES) to any column whose name
    matches a keyword. Checks both "object" and "str" dtype, since newer
    pandas versions use a dedicated "str" dtype for text columns.
    """
    for col in df.columns:
        col_lower = col.lower()
        for keyword, rule in CASING_RULES.items():
            if keyword in col_lower and str(df[col].dtype) in ("object", "str"):
                before_sample = df[col].dropna().unique()[:5]
                if rule == "lower":
                    df[col] = df[col].str.lower()
                elif rule == "title":
                    df[col] = df[col].str.title()
                elif rule == "upper":
                    df[col] = df[col].str.upper()
                log_step(log, "standardize_casing", f"{col} -> {rule} case (e.g. {list(before_sample)})")
                break
    return df


def apply_category_mapping(df, mappings, log):
    """
    Applies user-defined canonical mappings to fix values that mean the same
    thing but are spelled differently ("USA" vs "United States" vs "usa") --
    something casing alone can never fix.
    """
    for col, mapping in mappings.items():
        if col in df.columns:
            before_unmapped = df[col].dropna().unique()
            df[col] = df[col].replace(mapping)
            changed = set(before_unmapped) & set(mapping.keys())
            if changed:
                log_step(log, "category_value_mapping", f"{col}: mapped {list(changed)} -> canonical values")
    return df


def resolve_duplicate_columns(df, log, match_threshold=DUPLICATE_COLUMN_MATCH_THRESHOLD):
    """
    Handles pandas' auto-renamed duplicate columns (e.g. "email"/"email.1").
    Only removes the ".N" duplicate if it matches its base column at least
    match_threshold of the time -- otherwise both columns are kept and the
    mismatch is logged instead of silently dropped.
    """
    dupe_groups = {}
    for col in df.columns:
        match = re.match(r"^(.*)\.(\d+)$", str(col))
        if match:
            base = match.group(1)
            if base in df.columns:
                dupe_groups.setdefault(base, []).append(col)

    for base, dupes in dupe_groups.items():
        for dupe_col in dupes:
            same = (df[base] == df[dupe_col]) | (df[base].isna() & df[dupe_col].isna())
            match_rate = same.mean()
            if match_rate >= match_threshold:
                df = df.drop(columns=[dupe_col])
                log_step(log, "resolve_duplicate_columns", f"{dupe_col} removed -- {match_rate:.0%} match with {base}")
            else:
                mismatches = int((~same).sum())
                log_step(log, "resolve_duplicate_columns", f"{dupe_col} kept -- differs from {base} in {mismatches} row(s)")
    return df


def fix_encoding_issues(df, log):
    """
    Fixes common special-character / encoding artifacts in text fields --
    smart quotes, non-breaking spaces, en/em dashes, ellipsis characters,
    and general unicode compatibility normalization.
    """
    replacements = {
        "\u00a0": " ",
        "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-",
        "\u2026": "...",
    }
    affected_cols = []
    for col in df.select_dtypes(include=["object", "str"]).columns:
        before = df[col].copy()
        cleaned = df[col]
        for bad, good in replacements.items():
            cleaned = cleaned.str.replace(bad, good, regex=False)
        cleaned = cleaned.apply(lambda x: unicodedata.normalize("NFKC", x) if isinstance(x, str) else x)
        if not cleaned.equals(before):
            affected_cols.append(col)
        df[col] = cleaned
    log_step(log, "fix_encoding_issues", f"normalized special characters in: {affected_cols if affected_cols else 'none'}")
    return df
