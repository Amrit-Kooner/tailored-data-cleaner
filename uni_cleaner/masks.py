# masks.py
"""
The cell-flag masks that drive the Excel colour-coding:

    fail_mask           RED       value failed to parse/convert, kept as-is
    missing_mask         ORANGE    value is missing
    price_review_mask    YELLOW    zero/negative value in a numeric price column, or a
                                    text value in a numeric price column
    app_duplicate_mask   GREEN     an APP value appears more than once (own sheet)
    app_invalid_mask     LAVENDER  an APP value is blank, or a placeholder such as
                                    N/A / TBD / unknown (own sheet)
    disguised_mask       PURPLE    the cell holds placeholder text (N/A, TBD, unknown, -, ...).
                                    Kept, never removed -- just flagged, across every column.

Every mask here is built with a SINGLE pandas constructor call (see _overlay),
and build_disguised_mask / build_flag_masks take a blank_cols set so they skip
columns that carry no data instead of walking 22k NaN cells per column.
"""

import pandas as pd

from config import UNIQUE_ID_KEYWORDS
from .audit import log_step
from .detectors import looks_like_price
from .models import RESERVED_COLUMNS, SOURCE_FILE_COL
from .textutil import name_is_keyword, disguised_blank_mask


def empty_mask(df):
    return pd.DataFrame(False, index=df.index, columns=df.columns)


def _overlay(base, changes):
    """
    Return a boolean frame like `base` with the columns listed in `changes`
    replaced by their new arrays, built in ONE pandas constructor call.

    Why this exists (the fragmentation fix):

      `base[col] = arr` in a loop REPLACES one block per touched column, so
      a frame that started life as one contiguous block ends up with one
      tiny block per changed column. Once a frame passes ~100 internal
      blocks, pandas emits

          PerformanceWarning: DataFrame is highly fragmented.

      on the NEXT insert (i.e. the "source_file = False" line) and every
      later operation walks the block list instead of a single buffer.

      Rebuilding via a single pd.DataFrame(...) call keeps the result at one
      block from the start.
    """
    if not changes:
        return base
    data = {
        c: (changes[c] if c in changes else base[c].to_numpy())
        for c in base.columns
    }
    return pd.DataFrame(data, index=base.index)


def mark_failed(fail_mask, col, failed):
    """Flag every row where `failed` is True in column `col` of the fail mask."""
    if fail_mask is not None and failed.any():
        fail_mask.loc[failed[failed].index, col] = True


def _is_bool(value):
    """
    True for a Python / numpy bool. A boolean is NEVER treated as a number in
    the price checks: 0, False and a blank are three different values, so
    False is not coerced to 0 and flagged as a "zero price".
    """
    return isinstance(value, bool) or type(value).__name__ == "bool_"


def _is_bool_like(value):
    """A real boolean, or the text TRUE / FALSE (what a CSV file gives): both are 'a boolean'."""
    return _is_bool(value) or (isinstance(value, str) and value.strip().lower() in ("true", "false"))


def build_disguised_mask(df, log, exclude_cols=(), blank_cols=()):
    """
    PURPLE mask: every cell of the finished table that holds a disguised blank
    (placeholder text such as 'N/A', 'TBD', 'unknown', '-' -- see
    config.DISGUISED_BLANKS). Nothing is removed or blanked; the cells are only
    flagged so they can be checked. Returns (mask, {column: count}).

    `exclude_cols` -- the APP column(s) are excluded here: a placeholder APP
    value is already flagged on the lavender ID Anomalies sheet, and should
    not also show up (in purple) on the Disguised Blanks sheet.
    `blank_cols`   -- fully blank columns are skipped: a blank cell is not a
                      placeholder, so nothing there could ever be flagged.
    """
    exclude_cols = set(exclude_cols)
    blank_cols = set(blank_cols)
    found = {}
    by_column = {}
    for col in df.columns:
        if col in RESERVED_COLUMNS or col in exclude_cols or col in blank_cols:
            continue
        masked = disguised_blank_mask(df[col])
        if masked.any():
            found[col] = masked.to_numpy()
            by_column[col] = int(masked.sum())

    mask = _overlay(empty_mask(df), found)
    total = sum(by_column.values())
    if total:
        log_step(log, "flag_disguised_blanks",
                 f"{total} disguised blank cell(s) (placeholder text such as 'N/A', 'TBD', 'unknown', '-') "
                 f"were KEPT (not removed or blanked) and flagged for review (PURPLE) -- by column: {by_column}")
    else:
        log_step(log, "flag_disguised_blanks",
                 "0 disguised blank cell(s) (placeholder text such as 'N/A', 'TBD', 'unknown', '-') found")
    return mask, by_column


def build_flag_masks(df, log, blank_cols=()):
    """
    Returns:
      missing_mask,
      price_review_mask,
      price_cols_checked

    Price anomalies are kept separate from APP-number anomalies so the Excel
    output can put them on their own sheet and colour.

    Fully blank columns are skipped from the price check (blank_cols) -- they
    can't contain a price, and walking their 22k NaN cells per column was
    wasted work on wide sparse files.
    """
    missing_mask = df.isna()

    numeric_cols = df.select_dtypes(include=["number"]).columns
    for col in numeric_cols:
        missing = int(df[col].isna().sum())
        if missing > 0:
            log_step(log, "preserve_missing_numeric",
                     f"{col}: {missing} missing value(s) preserved as missing")

    blank_cols = set(blank_cols)
    price_cols = [
        c for c in df.columns
        if c not in RESERVED_COLUMNS and c not in blank_cols and looks_like_price(c)
    ]

    price_arrays = {}
    missing_overrides = {}
    for col in price_cols:
        present = df[col].notna()
        present_count = int(present.sum())
        if present_count == 0:
            continue

        if pd.api.types.is_bool_dtype(df[col]):
            continue
        non_null = df[col].dropna()
        if len(non_null) and non_null.map(_is_bool).all():
            continue

        try:
            numeric_values = pd.to_numeric(df[col], errors="coerce")
        except (TypeError, ValueError):
            numeric_values = pd.to_numeric(df[col].astype(str), errors="coerce")

        is_bool_val = df[col].map(_is_bool)
        numeric_mask = numeric_values.notna() & ~is_bool_val
        is_numeric_field = int(numeric_mask.sum()) >= present_count / 2

        placeholder = disguised_blank_mask(df[col])
        zero_or_negative = numeric_mask & (numeric_values <= 0)

        if is_numeric_field:
            non_numeric = (
                present
                & ~numeric_mask
                & ~placeholder
                & ~df[col].map(_is_bool_like).astype(bool)
            )
        else:
            non_numeric = pd.Series(False, index=df.index)

        flagged = zero_or_negative | non_numeric
        price_arrays[col] = flagged.to_numpy()
        missing_overrides[col] = (missing_mask[col] & ~flagged).to_numpy()

        n_flagged = int(flagged.sum())
        if n_flagged:
            n_zero = int(((numeric_values == 0) & zero_or_negative).sum())
            n_negative = int(((numeric_values < 0) & zero_or_negative).sum())
            n_non_numeric = int(non_numeric.sum())
            parts = []
            if n_zero or n_negative:
                parts.append(f"{n_zero} zero, {n_negative} negative")
            if n_non_numeric:
                parts.append(f"{n_non_numeric} not numeric (a string in a numeric field)")
            log_step(
                log,
                "flag_price_anomalies",
                f"{col}: {n_flagged} value(s) flagged for review -- "
                + "; ".join(parts),
            )

    price_review_mask = _overlay(empty_mask(df), price_arrays)
    missing_mask = _overlay(missing_mask, missing_overrides)
    return missing_mask, price_review_mask, price_cols


def _app_id_columns(df):
    """Exact APP columns only (app / app_num / app_no / app_number)."""
    return [
        c for c in df.columns
        if c not in RESERVED_COLUMNS
        and c != SOURCE_FILE_COL
        and name_is_keyword(c, UNIQUE_ID_KEYWORDS)
    ]


def flag_app_duplicates(df, app_duplicate_mask, log):
    """
    APP duplicate-value check only (own sheet, GREEN).

    A non-blank, non-placeholder APP value that appears more than once in an
    exact APP column is flagged here. Blank cells and disguised-blank
    placeholders (N/A, TBD, unknown, ...) are never counted as duplicates of
    each other -- those are flag_app_invalid's job instead.
    """
    id_cols = _app_id_columns(df)

    if not id_cols:
        log_step(
            log, "flag_app_duplicates",
            "skipped -- no APP column found (looked for exact names: "
            + ", ".join(UNIQUE_ID_KEYWORDS) + ")",
        )
        return app_duplicate_mask, id_cols

    overrides = {}
    for col in id_cols:
        series = df[col]
        blank_or_placeholder = (
            series.isna()
            | (series.astype(str).str.strip() == "")
            | disguised_blank_mask(series)
        )

        non_blank_values = series[~blank_or_placeholder].astype(str).str.strip()
        dupe = pd.Series(False, index=series.index)
        dupe.loc[non_blank_values[non_blank_values.duplicated(keep=False)].index] = True

        overrides[col] = (app_duplicate_mask[col] | dupe).to_numpy()

        n_dupe = int(dupe.sum())
        if n_dupe:
            log_step(
                log, "flag_app_duplicates",
                f"{col}: {n_dupe} duplicate value(s) flagged for review (own sheet) "
                f"-- APP field is expected to be unique",
            )
        else:
            log_step(log, "flag_app_duplicates", f"{col}: 0 duplicate value(s) found")

    return _overlay(app_duplicate_mask, overrides), id_cols


def flag_app_invalid(df, missing_mask, app_invalid_mask, log):
    """
    APP blank/invalid/unexpected check only (own sheet, LAVENDER).

    An APP value that is blank OR a disguised-blank placeholder (N/A, TBD,
    unknown, '-', ...) is flagged here. Duplicates are handled separately by
    flag_app_duplicates so the two issue types land on their own sheets.
    """
    id_cols = _app_id_columns(df)

    if not id_cols:
        log_step(
            log, "flag_app_invalid",
            "skipped -- no APP column found (looked for exact names: "
            + ", ".join(UNIQUE_ID_KEYWORDS) + ")",
        )
        return missing_mask, app_invalid_mask, id_cols

    invalid_overrides = {}
    missing_overrides = {}
    for col in id_cols:
        series = df[col]
        blank = series.isna() | (series.astype(str).str.strip() == "")
        placeholder = disguised_blank_mask(series) & ~blank

        invalid = blank | placeholder
        invalid_overrides[col] = (app_invalid_mask[col] | invalid).to_numpy()
        missing_overrides[col] = (missing_mask[col] & ~invalid).to_numpy()

        n_blank = int(blank.sum())
        n_placeholder = int(placeholder.sum())
        if n_blank or n_placeholder:
            log_step(
                log, "flag_app_invalid",
                f"{col}: {n_blank} blank + {n_placeholder} placeholder (e.g. N/A, TBD) "
                f"value(s) flagged for review (own sheet) -- APP field is expected to "
                f"be populated with a real value",
            )
        else:
            log_step(log, "flag_app_invalid", f"{col}: 0 blank/invalid value(s) found")

    app_invalid_mask = _overlay(app_invalid_mask, invalid_overrides)
    missing_mask = _overlay(missing_mask, missing_overrides)
    return missing_mask, app_invalid_mask, id_cols