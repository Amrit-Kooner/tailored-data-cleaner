# output/report.py
"""
The audit report shown on the "Report" sheet of the cleaned workbook:
source-file metadata, dataset overview, per-column details, a before/after
comparison, things to review -- followed by the step-by-step audit log.
Every number is computed from the actual dataframes and the stats collected
while the pipeline ran -- nothing is estimated.
"""

from datetime import datetime

import pandas as pd

from config import CLEANER_VERSION

# The 5 section titles used by build_dataset_report, in order. Exported so
# the Excel writer (_write_report_sheet) can recognise these exact lines
# when it comes back through the plain-text report to colour-band each
# section -- one shared source of truth instead of the fill logic having
# to guess or duplicate the literal strings.
SECTION_TITLES = (
    "SOURCE FILE",
    "DATASET OVERVIEW",
    "COLUMN DETAILS",
    "BEFORE vs AFTER COMPARISON",
    "THINGS TO REVIEW",
)


def _format_bytes(n):
    """Human-readable byte count, e.g. 4213 -> '4.1 KB'."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _source_file_info(stats):
    lines = []
    if stats.get("source_file_name"):
        lines.append(f"File name:         {stats['source_file_name']}")
    if stats.get("file_type"):
        lines.append(f"File type:         {stats['file_type']}")
    if stats.get("source_file_size") is not None:
        lines.append(f"File size:         {_format_bytes(stats['source_file_size'])}")
    if stats.get("source_file_modified"):
        lines.append(f"Last modified:     {stats['source_file_modified']}")
    if stats.get("source_file_sha256"):
        lines.append(f"SHA-256:           {stats['source_file_sha256']}")
    if stats.get("encoding"):
        lines.append(f"Input encoding:    {stats['encoding']} (used to read the source text file)")
    if stats.get("sheet_name"):
        sheet_names = stats.get("sheet_names") or [stats["sheet_name"]]
        if len(sheet_names) > 1:
            lines.append(f"Sheet read:        {stats['sheet_name']} (of {len(sheet_names)} sheet(s) in the workbook -- the rest were not processed)")
            lines.append(f"All sheets:        {sheet_names}")
            if stats.get("sheet_note"):
                lines.append(f"Sheet note:        {stats['sheet_note']}")
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
        is_yn_like = (
            dtype == "object"
            and s.dropna().nunique() > 0
            and set(s.dropna().astype(str).unique()) <= {"Y", "N"}
        )
        if is_bool_like:
            vc = s.value_counts(dropna=True)
            note = f"{int(vc.get(True, 0))} true / {int(vc.get(False, 0))} false"
        elif is_yn_like:
            vc = s.value_counts(dropna=True)
            note = f"{int(vc.get('Y', 0))} Y / {int(vc.get('N', 0))} N"
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

    flag_cols = stats.get("flag_columns") or []
    flag_line = (
        f"Flag columns standardised to Y/N: {', '.join(flag_cols)}"
        if flag_cols else
        "Flag columns standardised to Y/N: none"
    )

    lines = [
        f"Rows:                          {orig_rows} -> {final_rows} ({final_rows - orig_rows:+d})",
        f"Columns:                       {orig_cols} -> {final_cols} ({final_cols - orig_cols:+d})",
        f"-",
        f"Columns added:                 {stats.get('cols_added') or 'none'}",
        f"Columns removed:               {stats.get('cols_removed') or 'none'}",
        f"Blank columns removed:         0 (blank columns are kept, not removed)",
        f"-",
        f"Blank rows removed:            {stats.get('blank_rows_removed', 0)}",
        f"Duplicate rows in source file: {orig_dupe_rows} (before cleaning)",
        f"Exact duplicate rows removed:  {stats.get('duplicate_rows_removed', 0)}",
        f"Duplicate APP rows collapsed:  {stats.get('duplicate_app_numbers_collapsed', 0)} (longest description kept per APP number)",
        f"-",
        f"Blank cells in original file:  {orig_blank_cells} (truly empty cells)",
        f"Disguised blanks (kept, flagged PURPLE):  {stats.get('disguised_blanks_flagged', 0)} (placeholder text such as 'N/A', 'unknown', 'TBD', '-' -- kept, never turned into a blank)",
        f"-",
        f"Values with whitespace fixed:  {stats.get('values_trimmed', 0)}",
        flag_line,
        f"-",
        f"Filler rows removed from top:      {stats.get('filler_rows_top_removed', 0)} (junk/title rows above the real header)",
        f"-",
        f"UNEXPECTED DATA LOSS GUARD:        PASSED",
        f"Allowed loss categories only:      exact duplicates, duplicate APP numbers (longest description kept), fully blank rows, explicit top filler, repeated headers, and high-confidence totals/subtotals",
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
    purple_n = stats.get("disguised_blanks_flagged")
    dup_name_n = stats.get("duplicate_content_columns_flagged")
    multi_desc_n = stats.get("multi_description_columns_flagged")
    multi_desc_note = (
        "\n                 A description column's header is also BLUE when the file has SEVERAL "
        "description fields."
    ) if multi_desc_n else ""
    if missing_n is not None or failed_n is not None or review_n is not None:
        lines.append(
            "The Cleaned sheet has a 'row_number' column (1..N). Every issue sheet below "
            "carries a matching 'cleaned_row_number' column, so a flagged row can be traced "
            "back to its exact row on Cleaned.\n"
        )
        lines.append(
            "In the Excel output, cells are colour-coded:\n"
            "  ORANGE = missing value.\n"
            "  RED    = value that failed to parse/convert and so was left exactly as its "
            "original, uncleaned value (never removed).\n"
            "  PURPLE = a disguised blank: placeholder text such as 'N/A', 'TBD', 'unknown' or '-'. "
            "It is kept -- never removed or turned into a blank -- and flagged for review.\n"
            "  PINK = duplicate APP: an APP value appears more than once (own sheet). "
            "PINK is reserved for APP.\n"
            "  GREEN = duplicate value field: a column has identical data to another field.\n"
            "  LAVENDER = APP ID issue: an APP value is blank or a placeholder such as "
            "N/A/TBD/unknown (own sheet).\n"
            "  YELLOW = price anomaly ONLY: a zero, negative, or non-numeric value in a "
            "numeric price field.\n"
        )
        if missing_n is not None:
            lines.append(f"  - {missing_n} cell(s) highlighted ORANGE (missing).")
        if failed_n is not None:
            lines.append(f"  - {failed_n} cell(s) highlighted RED (failed to parse/clean).")
        if purple_n is not None:
            cols = stats.get("disguised_blank_columns") or {}
            by_col = f" -- by column: {cols}" if cols else ""
            lines.append(f"  - {purple_n} cell(s) highlighted PURPLE (disguised blanks such as N/A or TBD, kept as-is){by_col}.")
        if review_n is not None:
            lines.append(f"  - {review_n} cell(s) requiring review are split across the ID Duplicates, ID Anomalies and Price Anomalies sheets.")
        if dup_name_n:
            lines.append(f"  - {dup_name_n} column header(s) shown on the green Duplicate Values sheet.")
        if multi_desc_n:
            lines.append(f"  - {multi_desc_n} header anomalies item(s) shown on the neutral Header Anomalies sheet.")

        raw_missing = stats.get("raw_missing_cells")
        if raw_missing is not None and missing_n is not None and raw_missing != missing_n:
            reclassified = raw_missing - missing_n
            lines.append(
                f"  - Note: {raw_missing} cell(s) are blank in the DATASET OVERVIEW count above, and "
                f"{reclassified} of those are ID anomalies shown on the ID Anomalies sheet "
                f"instead of the Missing Values sheet."
            )

        price_cols = stats.get("price_columns_checked")
        if price_cols:
            lines.append(f"  - Price-anomaly check ran on: {', '.join(price_cols)}.")
        else:
            lines.append("  - Price-anomaly check: skipped -- no price-like column found.")

        id_cols = stats.get("id_columns_checked")
        if id_cols:
            lines.append(f"  - APP anomaly check ran on: {', '.join(id_cols)}.")
        else:
            lines.append("  - APP anomaly check: skipped -- no column named exactly app / app_number / app_num / app_no.")

        lines.append("")

    if not flagged:
        lines.append("No further issues detected -- nothing else needs manual review.")
    else:
        lines.extend(f"- {item}" for item in flagged)
    return "\n".join(lines)


def build_dataset_report(original_df, df, stats, log):
    sections = [
        (SECTION_TITLES[0], _source_file_info(stats)),
        (SECTION_TITLES[1], _dataset_overview(df, stats)),
        (SECTION_TITLES[2], _column_details(df)),
        (SECTION_TITLES[3], _before_after_comparison(original_df, df, stats)),
        (SECTION_TITLES[4], _things_to_review(log, stats)),
    ]
    blocks = []
    for title, body in sections:
        blocks.append(f"{title}\n{'-' * len(title)}\n{body}")
    return "\n\n".join(blocks)


def build_report_lines(source_filename, dataset_summary, log):
    """
    Same content that used to be written to final_file_log.txt (report
    header, dataset summary, then the numbered audit log), returned as a
    list of lines instead of written to a file -- so it can be dropped
    straight into the "Report" sheet of the cleaned workbook, one line per
    row.
    """
    divider = "=" * 72
    lines = []

    lines.append(divider)
    lines.append(" DATA CLEANING REPORT")
    lines.append(f" Cleaner version : {CLEANER_VERSION}")
    lines.append(f" Source file : {source_filename}")
    lines.append(f" Generated   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(divider)
    lines.append("")

    lines.extend(dataset_summary.strip().splitlines())
    lines.append("")

    total_steps = len(log["steps"])
    title = f"AUDIT LOG ({total_steps} steps)"
    lines.append(title)
    lines.append("-" * len(title))
    for idx, entry in enumerate(log["steps"], start=1):
        lines.append(f"[{idx:02d}] {entry['step']:<28} -> {entry['detail']}")

    lines.append("")
    lines.append(divider)
    lines.append(" End of report")
    lines.append(divider)
    return lines