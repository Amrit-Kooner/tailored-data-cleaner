# pipeline.py
"""
The conductor.
"""

import os
import re
import time
import pandas as pd

from config import DISGUISED_BLANKS, CATEGORY_MAPPINGS, EXCEL_ENGINES
from utils import (
    log_step, build_dataset_report, dedupe_names_with_suffixes,
    detect_text_encoding,
    assert_no_unexpected_value_loss,
)
from cleaning import (
    find_header_row, standardize_casing, apply_category_mapping,
    resolve_duplicate_columns, detect_duplicate_content_columns,
    fix_encoding_issues, format_phone_numbers,
    remove_repeated_header_rows, remove_total_rows,
)
from dtype_detection import (
    detect_date_columns, convert_to_uk_format, infer_and_fix_dtypes,
    looks_like_price,
)


EXCEL_EXTENSIONS = set(EXCEL_ENGINES.keys())

FILE_TYPE_LABELS = {
    "csv":  "CSV (comma-separated text)",
    "tsv":  "TSV (tab-separated text)",
    "txt":  "Plain text",
    "xlsx": "Excel workbook (.xlsx)",
    "xls":  "Excel workbook (.xls, legacy binary)",
    "xlsm": "Excel macro-enabled workbook (.xlsm)",
    "xlsb": "Excel binary workbook (.xlsb)",
}


def _open_excel(path):
    """(unchanged)"""
    ext = os.path.splitext(path)[1].lower()
    engine = EXCEL_ENGINES.get(ext)
    try:
        xl = pd.ExcelFile(path, engine=engine)
    except ImportError as e:
        package = engine or "the required engine package"
        raise RuntimeError(
            f"Cannot read {ext} file '{os.path.basename(path)}' -- the "
            f"'{package}' package is required. Install it with "
            f"'pip install {package}'."
        ) from e
    except Exception as e:
        raise RuntimeError(
            f"Could not open Excel file '{os.path.basename(path)}': {e}"
        ) from e
    return xl, list(xl.sheet_names)


def _detect_separator(path, encoding):
    """
    Best-effort field-separator detection for a text file so that:
      * a .tsv is actually read as tab-separated (previously every row
        collapsed into a single column), and
      * a European-style CSV using ';' (or a pipe-delimited export) is
        not silently collapsed either.

    FIX: the previous version only examined the FIRST non-empty line and,
    if that line happened to contain no separator (e.g. a title row above
    a ';'- or '|'-delimited file), it fell back to ',' and collapsed every
    data row into a single column. Now up to 20 non-empty lines are
    inspected and the separator with the highest total count across those
    lines wins. Falls back to ',' if none is found.
    """
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            sample = f.read(65536)
    except OSError:
        return ","

    sep_counts = {sep: 0 for sep in ("\t", ";", "|", ",")}
    lines_checked = 0
    for line in sample.splitlines():
        if not line.strip():
            continue
        for sep in sep_counts:
            sep_counts[sep] += line.count(sep)
        lines_checked += 1
        if lines_checked >= 20:
            break

    if not lines_checked:
        return ","
    best = max(sep_counts, key=sep_counts.get)
    if sep_counts[best] > 0:
        return best
    return ","


def clean_file(path):
    log = {"source_file": os.path.basename(path), "steps": []}
    start_time = time.time()

    ext = os.path.splitext(path)[1].lower()
    is_excel = ext in EXCEL_EXTENSIONS
    file_encoding = None
    sheet_name = None
    sheet_names = None
    xl = None

    if is_excel:
        xl, sheet_names = _open_excel(path)
        sheet_name = sheet_names[0] if sheet_names else 0
        try:
            raw_df = xl.parse(sheet_name=sheet_name, header=None)
        except Exception as e:
            xl.close()
            raise RuntimeError(
                f"Could not parse sheet '{sheet_name}' of "
                f"'{os.path.basename(path)}': {e}"
            ) from e
        read_sep = None
    else:
        file_encoding = detect_text_encoding(path)
        # FIX: TSV must be read with a tab separator; otherwise every row
        # collapses into a single column. For other text extensions, sniff
        # the first non-empty line so ';' / '|' exports are handled too.
        if ext == ".tsv":
            read_sep = "\t"
        else:
            read_sep = _detect_separator(path, file_encoding)
        raw_df = pd.read_csv(path, header=None, encoding=file_encoding, sep=read_sep)

    try:
        header_row = find_header_row(raw_df)
        if is_excel:
            original_df = xl.parse(sheet_name=sheet_name, header=header_row)
        else:
            original_df = pd.read_csv(path, header=header_row, encoding=file_encoding, sep=read_sep)
    finally:
        if xl is not None:
            xl.close()

    df = original_df.copy()
    log_step(log, "find_header_row", f"header found at row {header_row} ({header_row} junk row(s) skipped above it)")

    original_dtypes = {
        str(c).strip().lower().replace(" ", "_"): str(original_df[c].dtype) for c in original_df.columns
    }
    stats = {}
    stats["filler_rows_top_removed"] = header_row

    stats["source_file_name"] = os.path.basename(path)
    stats["file_type"] = FILE_TYPE_LABELS.get(ext.lstrip("."), ext.upper() or "Unknown")
    try:
        file_stat = os.stat(path)
        stats["source_file_size"] = file_stat.st_size
        stats["source_file_modified"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(file_stat.st_mtime)
        )
    except OSError:
        pass
    if file_encoding is not None:
        stats["encoding"] = file_encoding
    if sheet_name is not None:
        stats["sheet_name"] = str(sheet_name)
        stats["sheet_names"] = [str(s) for s in (sheet_names or [])]

    renamed = [col for col in df.columns if re.search(r"\.\d+$", str(col))]
    log_step(log, "check_duplicate_fields", f"{len(renamed)} duplicate column name(s) flagged: {renamed}")

    unnamed_cols = [col for col in df.columns if pd.isna(col) or str(col).startswith("Unnamed")]
    log_step(log, "check_unnamed_columns", f"{len(unnamed_cols)} unnamed column(s) flagged: {unnamed_cols}")

    # --- BUG FIX: never overwrite an existing source_file column. ---
    # If the source file already has one or more columns that normalize to
    # "source_file" (e.g. "Source File", "SOURCE_FILE"), rename EVERY one
    # of them so their values survive. The earlier version only renamed the
    # first match, so a second matching column would silently collide with
    # the pipeline's own "source_file" column downstream.
    existing_normalized = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    source_file_indices = [i for i, n in enumerate(existing_normalized) if n == "source_file"]
    if source_file_indices:
        renames = {}
        used = set(str(c) for c in df.columns)
        for i in source_file_indices:
            original_col = df.columns[i]
            new_name = "source_file_original"
            counter = 1
            while new_name in used:
                new_name = f"source_file_original_{counter}"
                counter += 1
            renames[original_col] = new_name
            used.add(new_name)
        df = df.rename(columns=renames)
        log_step(log, "preserve_existing_source_file",
                 f"{len(renames)} existing column(s) renamed so their original values are "
                 f"not overwritten by the pipeline's source_file column: {renames}")

    df["source_file"] = os.path.basename(path)
    log_step(log, "add_source_file_column", "added 'source_file' column recording which file each row came from")

    # --- BUG FIX: coerce headers to str BEFORE the .str accessor. ---
    # If the real header row contains a non-string cell (e.g. an Excel
    # header whose year cell reads 2024 as an integer), df.columns is a
    # non-string Index and `.str.strip()` either raises or silently yields
    # NaN for the non-string entries, producing unusable column names
    # downstream. astype(str) guarantees every header is a string first.
    old_cols = list(df.columns)
    df.columns = (
        df.columns.astype(str)
        .str.strip()
        .str.lower()
        .str.replace(" ", "_", regex=False)
    )
    renamed_pairs = [f"'{o}' -> '{n}'" for o, n in zip(old_cols, df.columns) if str(o) != n]
    log_step(log, "standardize_column_names",
             f"{len(renamed_pairs)} column name(s) standardized" + (f": {renamed_pairs}" if renamed_pairs else ""))

    deduped_names = dedupe_names_with_suffixes(list(df.columns))
    dupe_renames = [f"'{o}' -> '{n}'" for o, n in zip(df.columns, deduped_names) if o != n]
    df.columns = deduped_names
    log_step(log, "dedupe_standardized_column_names",
             f"{len(dupe_renames)} column name(s) that collided after standardization "
             f"given .N suffixes" + (f": {dupe_renames}" if dupe_renames else ""))

    df = remove_repeated_header_rows(df, log)
    df = remove_total_rows(df, log)

    # --- trim whitespace (value-only; guard it) ---
    stats["values_trimmed"] = 0
    trimmed_cols = []

    def strip_and_count(value):
        if isinstance(value, str):
            stripped = value.strip()
            if stripped != value:
                stats["values_trimmed"] += 1
                return stripped
        return value

    before_trim = df.copy()
    for col in df.select_dtypes(include=["object"]).columns:
        before_count = stats["values_trimmed"]
        df[col] = df[col].map(strip_and_count)
        if stats["values_trimmed"] > before_count:
            trimmed_cols.append(col)
    assert_no_unexpected_value_loss(before_trim, df, "trim_whitespace")  # GUARD
    log_step(log, "trim_whitespace",
             f"{stats['values_trimmed']} value(s) had leading/trailing whitespace removed"
             + (f" in columns: {trimmed_cols}" if trimmed_cols else ""))

    # --- fix encoding (value-only; guard it) ---
    before = df.copy()
    df = fix_encoding_issues(df, log)
    assert_no_unexpected_value_loss(before, df, "fix_encoding_issues")  # GUARD

    # --- phone numbers (value-only; guard it) ---
    before = df.copy()
    df = format_phone_numbers(df, log)
    assert_no_unexpected_value_loss(before, df, "format_phone_numbers")  # GUARD

    # --- disguised blanks (intentional loss; the guard knows which values
    #     are allowed to become NA) ---
    before_na = df.isna()
    before = df.copy()
    df = df.replace(DISGUISED_BLANKS, pd.NA)
    assert_no_unexpected_value_loss(  # GUARD
        before, df, "convert_disguised_blanks", disguised_blanks=DISGUISED_BLANKS
    )
    newly_blanked = (df.isna() & ~before_na).sum()
    newly_blanked = newly_blanked[newly_blanked > 0]
    stats["disguised_blanks_converted"] = int(newly_blanked.sum())
    detail = f"{stats['disguised_blanks_converted']} disguised blank value(s) (e.g. 'N/A', 'unknown', 'TBD') converted to real missing values"
    if not newly_blanked.empty:
        detail += f" -- by column: {newly_blanked.to_dict()}"
    log_step(log, "convert_disguised_blanks", detail)

    missing_count = df.isna().sum(axis=1)
    rows_with_missing = int((missing_count > 0).sum())
    log_step(log, "missing_value_summary", f"{rows_with_missing} row(s) contain at least one missing field after cleaning")

    # --- casing (value-only; guard it) ---
    before = df.copy()
    df = standardize_casing(df, log)
    assert_no_unexpected_value_loss(before, df, "standardize_casing")  # GUARD

    # --- category mapping (value-only; guard it) ---
    before = df.copy()
    df = apply_category_mapping(df, CATEGORY_MAPPINGS, log)
    assert_no_unexpected_value_loss(before, df, "apply_category_mapping")  # GUARD

    df = resolve_duplicate_columns(df, log)

    rows_before = len(df)
    df = df.drop_duplicates()
    stats["duplicate_rows_removed"] = rows_before - len(df)
    log_step(log, "drop_duplicate_rows", f"{stats['duplicate_rows_removed']} duplicate row(s) removed")

    rows_before, cols_before = df.shape
    data_cols = [c for c in df.columns if c != "source_file"]
    if data_cols:
        blank_row_mask = df[data_cols].isna().all(axis=1)
        df = df.loc[~blank_row_mask].reset_index(drop=True)
    df = df.dropna(axis=1, how="all")
    stats["blank_rows_removed"] = rows_before - df.shape[0]
    stats["blank_cols_removed"] = cols_before - df.shape[1]
    log_step(log, "drop_blank_rows_and_columns",
             f"{stats['blank_rows_removed']} blank row(s), {stats['blank_cols_removed']} blank column(s) removed")

    duplicate_name_map = detect_duplicate_content_columns(df, log)

    fail_mask = pd.DataFrame(False, index=df.index, columns=df.columns)

    # --- date conversion (value-only; guard it) ---
    date_cols = detect_date_columns(df)
    before = df.copy()
    df = convert_to_uk_format(df, date_cols, log, fail_mask)
    assert_no_unexpected_value_loss(before, df, "convert_to_uk_format")  # GUARD
    log_step(log, "format_dates", f"{len(date_cols)} date column(s) converted to dd/mm/yyyy: {date_cols}")

    # --- dtype inference (value-only; guard it) ---
    before = df.copy()
    df = infer_and_fix_dtypes(df, log, fail_mask)
    assert_no_unexpected_value_loss(before, df, "infer_and_fix_dtypes")  # GUARD

    missing_mask = df.isna()

    numeric_cols = df.select_dtypes(include=["number"]).columns
    for col in numeric_cols:
        missing = int(df[col].isna().sum())
        if missing > 0:
            log_step(log, "preserve_missing_numeric", f"{col}: {missing} missing value(s) preserved as missing")

    review_mask = pd.DataFrame(False, index=df.index, columns=df.columns)
    price_cols = [
        c for c in df.columns
        if c not in ("source_file", "missing_count") and looks_like_price(c)
    ]
    for col in price_cols:
        try:
            numeric_values = pd.to_numeric(df[col], errors="coerce")
        except (TypeError, ValueError):
            numeric_values = pd.to_numeric(df[col].astype(str), errors="coerce")

        flagged = numeric_values.notna() & (numeric_values <= 0)
        review_mask[col] = flagged
        missing_mask[col] = missing_mask[col] & ~flagged
        n_flagged = int(flagged.sum())
        if n_flagged:
            n_zero = int(((numeric_values == 0) & flagged).sum())
            n_negative = int(((numeric_values < 0) & flagged).sum())
            detail = f"{col}: {n_flagged} value(s) flagged for review (BLUE) -- {n_zero} zero, {n_negative} negative"
            log_step(log, "flag_zero_or_negative_price", detail)

    rows_diff = df.shape[0] - original_df.shape[0]
    cols_diff = df.shape[1] - original_df.shape[1]
    original_cols = set(original_dtypes.keys())
    final_cols = set(df.columns)
    cols_added = sorted(final_cols - original_cols)
    cols_removed = sorted(original_cols - final_cols)
    stats["cols_added"] = cols_added
    stats["cols_removed"] = cols_removed

    dtype_changes = []
    for col in df.columns:
        if col in original_dtypes and original_dtypes[col] != str(df[col].dtype):
            dtype_changes.append((col, original_dtypes[col], str(df[col].dtype)))
    stats["dtype_changes"] = dtype_changes

    log_step(log, "compare_original_to_final",
             f"original: {original_df.shape[0]} rows / {original_df.shape[1]} cols | "
             f"final: {df.shape[0]} rows / {df.shape[1]} cols | "
             f"row change: {rows_diff:+d}, column change: {cols_diff:+d} | "
             f"columns added: {cols_added if cols_added else 'none'} | "
             f"columns removed: {cols_removed if cols_removed else 'none'}")

    stats["missing_cells_highlighted"] = int(missing_mask.sum().sum())
    stats["failed_parse_cells_highlighted"] = int(fail_mask.sum().sum())
    stats["review_cells_highlighted"] = int(review_mask.sum().sum())
    stats["duplicate_content_columns_flagged"] = len(duplicate_name_map)

    stats["processing_seconds"] = round(time.time() - start_time, 2)
    dataset_summary = build_dataset_report(original_df, df, stats, log)
    return df, original_df, log, dataset_summary, date_cols, missing_mask, fail_mask, review_mask, duplicate_name_map