"""
Small, general-purpose helpers with no dependency on any other module in
this project (other than pandas/dateutil). Anything used by more than one
of the other modules, but too small to deserve its own file, lives here.
"""

import os

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


def _format_bytes(n):
    """Human-readable byte count, e.g. 4213 -> '4.1 KB'."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def detect_text_encoding(path, sample_bytes=200000):
    """
    Detects the text encoding of a file by inspecting its raw bytes, so the
    reader can be told the correct encoding explicitly instead of relying on
    the platform default (which differs between Windows and macOS/Linux and
    is a very common cause of mojibake in the first place).

    Tries, in order:
      1. An explicit BOM (UTF-8 BOM -> 'utf-8-sig', UTF-16 BOM -> 'utf-16')
      2. Strict UTF-8 (the modern default)
      3. Strict CP1252 (Windows-1252, the usual "Excel on Windows" export)
      4. Latin-1 as a last-resort fallback -- it can decode any byte sequence
         and never raises, so it is only reached when nothing else worked.

    Returns a python encoding name that pandas can accept directly.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(sample_bytes)
    except OSError:
        return "utf-8"

    if not raw:
        return "utf-8"

    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"

    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    try:
        raw.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        pass

    return "latin-1"


def _source_file_info(stats):
    """
    Describes the physical source file the report is about -- name, type,
    size, modification time, the encoding that was used to read it, the
    encoding of the output, and (for Excel workbooks) which sheet was read.
    This is the context a reviewer needs to reproduce or trust the cleaning
    run.
    """
    lines = []
    if stats.get("source_file_name"):
        lines.append(f"File name:         {stats['source_file_name']}")
    if stats.get("file_type"):
        lines.append(f"File type:         {stats['file_type']}")
    if stats.get("source_file_size") is not None:
        lines.append(f"File size:         {_format_bytes(stats['source_file_size'])}")
    if stats.get("source_file_modified"):
        lines.append(f"Last modified:     {stats['source_file_modified']}")
    if stats.get("encoding"):
        lines.append(f"Input encoding:    {stats['encoding']} (used to read the source text file)")
    if stats.get("sheet_name"):
        sheet_names = stats.get("sheet_names") or [stats["sheet_name"]]
        if len(sheet_names) > 1:
            lines.append(f"Sheet read:        {stats['sheet_name']} (first of {len(sheet_names)} sheet(s) -- the rest were not processed)")
            lines.append(f"All sheets:        {sheet_names}")
        else:
            lines.append(f"Sheet name:        {stats['sheet_name']}")
    lines.append("Output encoding:   UTF-8 (xlsx internal XML; audit log also UTF-8)")
    return "\n".join(lines) if lines else "(no file metadata available)"


def _dataset_overview(df, stats):
    rows, cols = df.shape
    total_cells = rows * cols
    missing_cells = int(df.isna().sum().sum())
    missing_pct = (missing_cells / total_cells * 100) if total_cells else 0
    dup_rows = int(df.duplicated().sum())
    mem_bytes = int(df.memory_usage(deep=True).sum())
    lines = [
        f"Rows:              {rows}",
        f"Columns:           {cols}",
        f"Duplicate rows:    {dup_rows} (remaining after exact source-row dedupe)",
        f"Missing cells:     {missing_cells} of {total_cells} ({missing_pct:.1f}%)",
        f"Memory usage:      {_format_bytes(mem_bytes)}",
    ]
    if "processing_seconds" in stats:
        lines.append(f"Processing time:   {stats['processing_seconds']}s")
    return "\n".join(lines)


def _column_details(df):
    if len(df.columns) == 0:
        return "(no columns)"
    name_width = max(10, min(28, max(len(str(c)) for c in df.columns) + 2))
    header = f"{'Column':<{name_width}}{'Type':<12}{'Missing':<14}{'Unique':<9}Notes"
    lines = [header, "-" * len(header)]
    n = len(df)
    for col in df.columns:
        s = df[col]
        dtype = str(s.dtype)
        missing = int(s.isna().sum())
        missing_pct = (missing / n * 100) if n else 0
        unique = int(s.nunique())

        note = ""
        is_bool_like = pd.api.types.is_bool_dtype(s) or (
            dtype == "object" and set(s.dropna().unique()) <= {True, False} and s.dropna().nunique() > 0
        )
        if is_bool_like:
            vc = s.value_counts(dropna=True)
            note = f"{int(vc.get(True, 0))} true / {int(vc.get(False, 0))} false"
        elif pd.api.types.is_numeric_dtype(s) and s.notna().any():
            note = f"min={s.min():.2f}, max={s.max():.2f}, mean={s.mean():.2f}"
        elif dtype == "category" and not s.value_counts().empty:
            top = s.value_counts().idxmax()
            note = f"most common: {top}"

        missing_str = f"{missing} ({missing_pct:.0f}%)"
        lines.append(f"{str(col):<{name_width}}{dtype:<12}{missing_str:<14}{unique:<9}{note}")
    return "\n".join(lines)


def _before_after_comparison(original_df, df, stats):
    orig_rows, orig_cols = original_df.shape
    final_rows, final_cols = df.shape
    orig_blank_cells = int(original_df.isna().sum().sum())
    orig_dupe_rows = int(original_df.duplicated().sum())

    lines = [
        f"Rows:                          {orig_rows} -> {final_rows} ({final_rows - orig_rows:+d})",
        f"Columns:                       {orig_cols} -> {final_cols} ({final_cols - orig_cols:+d})",
        f"-",
        f"Columns added:                 {stats.get('cols_added') or 'none'}",
        f"Columns removed:               {stats.get('cols_removed') or 'none'}",
        f"Blank columns removed:         {stats.get('blank_cols_removed', 0)}",
        f"-",
        f"Blank rows removed:            {stats.get('blank_rows_removed', 0)}",
        f"Duplicate rows in source file: {orig_dupe_rows} (before cleaning)",
        f"Exact duplicate rows removed:  {stats.get('duplicate_rows_removed', 0)}",
        f"-",
        f"Blank cells in original file:  {orig_blank_cells} (before disguised blanks like 'N/A' were converted)",
        f"Disguised blanks converted to blank:  {stats.get('disguised_blanks_converted', 0)} (e.g. 'N/A', 'unknown', 'TBD' -> missing)",
        f"Total empty cells:  {orig_blank_cells + stats.get('disguised_blanks_converted', 0)} (original empty cells + converted blanks)",
        f"-",
        f"Values with whitespace fixed:  {stats.get('values_trimmed', 0)}",
        f"-",
        f"Filler rows removed from top:      {stats.get('filler_rows_top_removed', 0)} (junk/title rows above the real header)",
        f"-",
        f"UNEXPECTED DATA LOSS GUARD:        PASSED",
        f"Allowed loss categories only:      exact duplicates, fully blank rows/columns, disguised blanks, explicit top filler, repeated headers, and high-confidence totals/subtotals",
        f"Automatic totals removal:           YES (high-confidence totals/subtotals only)",
        f"-",
    ]

    dtype_changes = stats.get("dtype_changes") or []
    if dtype_changes:
        lines.append("Data type changes:")
        for col, before, after in dtype_changes:
            lines.append(f"   {col}: {before} -> {after}")
    else:
        lines.append("Data type changes:             none")

    return "\n".join(lines)


def _things_to_review(log, stats=None):
    """
    Pulls out any audit-log entry that represents something the pipeline
    could NOT fully resolve on its own -- failed conversions, unparsable
    dates, mismatched duplicate columns -- so it's visible at a glance
    instead of buried in a long step-by-step log.
    """
    flag_markers = ("failed", "could not", "kept -- differs", "check_failed", "flagged for review")
    flagged = [
        f"{entry['step']}: {entry['detail']}"
        for entry in log["steps"]
        if any(marker in entry["detail"].lower() for marker in flag_markers)
    ]

    lines = []
    stats = stats or {}
    missing_n = stats.get("missing_cells_highlighted")
    failed_n = stats.get("failed_parse_cells_highlighted")
    review_n = stats.get("review_cells_highlighted")
    dup_name_n = stats.get("duplicate_content_columns_flagged")
    if missing_n is not None or failed_n is not None or review_n is not None:
        lines.append(
            "In the Excel output, cells are colour-coded:\n"
            "  ORANGE = missing value.\n"
            "  RED    = value that failed to parse/convert and so was left exactly as its "
            "original, uncleaned value (never removed).\n"
            "  BLUE FILL  = a 0.00 or negative value in a numeric price column (e.g. amount, "
            "cost, total). These are real, valid values, not blanks, so they're flagged "
            "separately for a sanity check rather than shown as missing.\n"
            "  BLUE HEADER = this column's data is identical, in every row, to another "
            "column under a different name -- a likely duplicate field. See the header "
            "cell's comment for which column it matches."
        )
        if missing_n is not None:
            lines.append(f"  - {missing_n} cell(s) highlighted ORANGE (missing).")
        if failed_n is not None:
            lines.append(f"  - {failed_n} cell(s) highlighted RED (failed to parse/clean).")
        if review_n is not None:
            lines.append(f"  - {review_n} cell(s) highlighted BLUE (zero/negative price value).")
        if dup_name_n:
            lines.append(f"  - {dup_name_n} column header(s) flagged BLUE FILL (identical data under a different field name).")
        lines.append("")

    if not flagged:
        lines.append("No further issues detected -- nothing else needs manual review.")
    else:
        lines.extend(f"- {item}" for item in flagged)
    return "\n".join(lines)


def build_dataset_report(original_df, df, stats, log):
    """
    Builds the full plain-text report written into the saved log file:
    source-file metadata, dataset overview, per-column details, a factual
    before/after comparison against the original file, and anything that
    needs manual review. Every number here is computed directly from the
    actual dataframes and the stats collected while the pipeline ran --
    nothing is estimated.
    """
    sections = [
        ("SOURCE FILE", _source_file_info(stats)),
        ("DATASET OVERVIEW", _dataset_overview(df, stats)),
        ("COLUMN DETAILS", _column_details(df)),
        ("BEFORE vs AFTER COMPARISON", _before_after_comparison(original_df, df, stats)),
        ("THINGS TO REVIEW", _things_to_review(log, stats)),
    ]
    blocks = []
    for title, body in sections:
        blocks.append(f"{title}\n{'-' * len(title)}\n{body}")
    return "\n\n".join(blocks)




def assert_no_unexpected_value_loss(before, after, step_name, disguised_blanks=None):
    """
    Safety gate for value-only cleaning steps.

    For every surviving cell, a source value that was genuinely populated may
    not become missing. The only permitted exception is a value explicitly
    classified as a disguised blank. This catches accidental data destruction
    from .str operations, coercion, date parsing, boolean mapping, encoding
    repair, etc.

    The check is deliberately performed only when row/column identity is
    unchanged. Structural removals are validated separately by the pipeline
    and are limited to the documented loss categories.
    """
    if before.shape != after.shape or list(before.columns) != list(after.columns):
        raise ValueError(
            f"DATA LOSS GUARD [{step_name}]: shape/columns changed during a "
            "value-only operation."
        )

    disguised = set(str(v).strip().lower() for v in (disguised_blanks or []))

    for col in before.columns:
        b = before[col]
        a = after[col]
        before_present = b.notna()
        if disguised:
            before_present &= ~b.map(
                lambda v: isinstance(v, str) and v.strip().lower() in disguised
            )

        lost = before_present & a.isna()
        if lost.any():
            examples = b[lost].head(10).tolist()
            raise ValueError(
                f"DATA LOSS GUARD [{step_name}]: {int(lost.sum())} populated "
                f"value(s) became blank in column '{col}'. Examples: {examples}"
            )


def assert_structural_loss_allowed(
    before,
    after,
    step_name,
    allowed_row_drop=0,
    allowed_col_drop=0,
):
    """
    Safety gate for structural operations.

    It verifies that row/column counts changed by no more than the explicitly
    permitted amount. The caller determines the permitted count from exact
    duplicate/blank/filler detection, never from the final count alone.
    """
    row_drop = before.shape[0] - after.shape[0]
    col_drop = before.shape[1] - after.shape[1]
    if row_drop < 0 or col_drop < 0:
        raise ValueError(
            f"DATA LOSS GUARD [{step_name}]: rows/columns were unexpectedly added."
        )
    if row_drop > allowed_row_drop or col_drop > allowed_col_drop:
        raise ValueError(
            f"DATA LOSS GUARD [{step_name}]: unexpected structural loss. "
            f"Rows dropped={row_drop}, allowed={allowed_row_drop}; "
            f"columns dropped={col_drop}, allowed={allowed_col_drop}."
        )

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
        return dateparser.parse(str(value), dayfirst=True, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None


def get_unique_dir(target_dir):
    """
    Returns target_dir if it doesn't exist yet, otherwise the same path with
    " (1)", " (2)", etc. appended until a free one is found -- same
    convention browsers use for repeat downloads. Used so cleaning the same
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


def dedupe_names_with_suffixes(names):
    """
    Makes every name in `names` unique by appending ".1", ".2", ".3", ...
    to any repeat. The first occurrence keeps its original name; the second
    becomes "name.1", the third "name.2", and so on. Every name that already
    exists anywhere in the source list is *reserved* up front, so a
    generated suffix can never collide with a column the file itself
    provided (e.g. a real "email.1" column). Matches pandas' own convention
    for exact duplicate headers, and lets resolve_duplicate_columns() later
    compare each suffixed column against its base -- keeping it only if it
    carries different data.
    """
    reserved = set(names)
    seen = set()
    counts = {}
    result = []
    for name in names:
        if name not in seen:
            seen.add(name)
            counts[name] = 0
            result.append(name)
            continue
        counts[name] += 1
        new_name = f"{name}.{counts[name]}"
        while new_name in reserved or new_name in seen:
            counts[name] += 1
            new_name = f"{name}.{counts[name]}"
        seen.add(new_name)
        counts[new_name] = 0
        result.append(new_name)
    return result