# steps/dtypes.py
"""
Works out what each column actually IS (ID, email, phone, currency,
percentage, flag, price, category, text) and converts it to the right
type. Anything that fails to convert is left as its original value and
flagged RED -- never dropped.

Fully blank columns are skipped by ctx.blank_cols (fast set lookup)
instead of walking 22k NaN cells at the top of every column.
"""

import re
import pandas as pd

from ..audit import log_step
from ..detectors import (
    looks_like_id, looks_like_email, looks_like_phone, looks_like_currency,
    looks_like_percentage, looks_like_boolean, looks_like_price, looks_like_measure,
    looks_like_category,
    flag_words_for, flag_keys,
)
from ..guards import value_only
from ..masks import mark_failed, _is_bool
from ..models import RESERVED_COLUMNS
from ..textutil import to_clean_text, disguised_blank_mask


def strip_currency_formatting(series):
    """'£1,234.50' / '(12.00)' / '1.234,56' / '5-' -> a string pd.to_numeric can parse."""
    def clean(value):
        if pd.isna(value):
            return ""
        s = str(value).strip()
        # Excel-style parenthesised negative
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1].strip()
        elif s.endswith("-") and not s.startswith("-"):
            s = "-" + s[:-1].strip()
        # Unicode minus / dash variants -> plain hyphen-minus
        s = (s.replace("\u2212", "-")
               .replace("\u2013", "-")
               .replace("\u2014", "-"))
        # Currency symbols
        s = re.sub(r"[£$€¥]", "", s)

        has_comma = "," in s
        has_dot = "." in s
        if has_comma and has_dot:
            if s.rfind(",") > s.rfind("."):
                # European: "1.234,56" -> "1234.56"
                s = s.replace(".", "").replace(",", ".")
            else:
                # UK/US: "1,234.56" -> "1234.56"
                s = s.replace(",", "")
        elif has_comma:
            parts = s.split(",")
            if len(parts) == 2 and len(parts[1]) == 2:
                # "1,23" -> decimal comma
                s = s.replace(",", ".")
            elif all(len(p) == 3 for p in parts[1:]):
                # "1,234" or "1,234,567" -> thousands
                s = s.replace(",", "")
            # Anything else (e.g. "1,2,3") is left with the comma so
            # pd.to_numeric fails cleanly instead of guessing.
        return s.strip()

    return series.map(clean)


def _apply_conversion(df, col, series, converted, ctx, step, label, failure_phrase="failed to convert"):
    """
    Writes `converted` into df[col], but keeps the ORIGINAL value wherever the
    conversion failed, flags those cells RED, and logs what happened.
    """
    placeholder = disguised_blank_mask(series)
    failed = converted.isna() & series.notna() & ~placeholder
    keep = failed | placeholder
    if keep.any() and series[keep].map(_is_bool).any():
        # A real TRUE / FALSE that is kept as-is must stay a boolean: putting it
        # back into a float column would silently turn it into 1.0 / 0.0, and
        # FALSE is not the number 0. The column becomes mixed (object) instead.
        df[col] = converted.astype(object).where(~keep, series)
    else:
        df[col] = converted.where(~keep, series)
    detail = f"{col} -> {label}"
    if failed.any():
        bad_values = series[failed].unique().tolist()
        detail += (f", {int(failed.sum())} value(s) {failure_phrase} and were "
                   f"left as-is (flagged RED): {bad_values}")
    log_step(ctx.log, step, detail)
    mark_failed(ctx.fail_mask, col, failed)


def _flag_map(col_name):
    """
    The output of a flag column: Y for every true-side value, N for every
    false-side value. Built from the same word lists the detector uses (see
    detectors.flag_words_for), so the two can never drift apart.
    """
    true_words, false_words = flag_words_for(col_name)
    mapping = {v: "Y" for v in true_words}
    mapping.update({v: "N" for v in false_words})
    return mapping


_KIND_LABELS = {"price": "price", "measure": "measurement"}


def _convert_named_numeric(df, col, series, ctx, kind):
    """
    Columns whose NAME says price ("price") or measurement ("measure").
    Returns True if the column was converted; otherwise logs why not.
    """
    label = _KIND_LABELS[kind]
    converted = pd.to_numeric(strip_currency_formatting(series), errors="coerce")
    placeholder = disguised_blank_mask(series)
    non_null = (series.notna() & ~placeholder).sum()
    success_rate = converted.notna().sum() / non_null if non_null > 0 else 0

    if success_rate >= 0.5:
        _apply_conversion(df, col, series, converted, ctx,
                          f"dtype_{kind}_numeric", f"converted to numeric ({label} field)")
        return True

    failed = converted.isna() & series.notna() & ~placeholder
    bad_values = series[failed].unique().tolist()
    log_step(ctx.log, f"dtype_{kind}_check_failed",
             f"{col} looks like a {label} column but only {success_rate:.0%} of values "
             f"converted -- left as text, nothing changed. Bad value(s): {bad_values}")
    return False


@value_only()
def infer_and_fix_dtypes(df, ctx):
    """
    One pass over the columns; the FIRST matching rule wins:
    ID -> email -> phone -> currency -> percentage -> flag (Y/N) -> price-named
    -> measurement-named -> category -> free text.

    Fully blank columns are skipped via ctx.blank_cols -- a set lookup,
    instead of the old `series.dropna()` per column.
    """
    log = ctx.log
    skipped_blank = 0
    for col in df.columns:
        series = df[col]
        # Fast path: entirely blank column, skip everything.
        if col in ctx.blank_cols:
            skipped_blank += 1
            continue
        if col in RESERVED_COLUMNS:
            continue

        # Placeholder text ('N/A', 'TBD', ...) says nothing about what kind of column this is.
        sample = series[series.notna() & ~disguised_blank_mask(series)].astype(str)
        if sample.empty:
            log_step(log, "dtype_skip_placeholder_column",
                     f"{col}: every value is a placeholder (N/A, TBD, ...) -- type inference skipped, values kept")
            continue

        if looks_like_id(col, series, sample):
            df[col] = to_clean_text(series)
            log_step(log, "dtype_id", f"{col} -> treated as text ID")
            continue

        if looks_like_email(series, sample):
            df[col] = to_clean_text(series).str.lower()
            log_step(log, "dtype_email", f"{col} -> treated as text email")
            continue

        if looks_like_phone(col, series, sample):
            df[col] = to_clean_text(series)
            log_step(log, "dtype_phone", f"{col} -> treated as text phone number")
            continue

        if looks_like_currency(series, sample):
            converted = pd.to_numeric(strip_currency_formatting(series), errors="coerce")
            _apply_conversion(df, col, series, converted, ctx,
                              "dtype_currency", "converted to numeric")
            ctx.money_cols.append(col)
            continue

        if looks_like_percentage(series, sample):
            cleaned = series.astype(str).str.replace("%", "", regex=False).str.strip()
            converted = pd.to_numeric(cleaned, errors="coerce")
            _apply_conversion(df, col, series, converted, ctx,
                              "dtype_percentage", "converted to numeric (% stripped)")
            continue

        if looks_like_boolean(col, series, sample):
            mapped = flag_keys(series).map(_flag_map(col))
            _apply_conversion(df, col, series, mapped, ctx,
                              "dtype_flag", "converted to flag (Y/N)",
                              failure_phrase="could not be mapped to Y or N")
            ctx.stats.setdefault("flag_columns", []).append(col)
            continue

        if looks_like_price(col) and _convert_named_numeric(df, col, series, ctx, "price"):
            ctx.money_cols.append(col)
            continue

        if looks_like_measure(col) and _convert_named_numeric(df, col, series, ctx, "measure"):
            continue

        if looks_like_category(series):
            df[col] = series.astype("category")
            log_step(log, "dtype_category", f"{col} -> converted to category ({series.nunique()} unique values)")
            continue

        df[col] = to_clean_text(series)
        log_step(log, "dtype_text", f"{col} -> left as free text")

    if skipped_blank:
        log_step(log, "dtype_skip_blank_columns",
                 f"{skipped_blank} fully blank column(s) skipped by type inference")
    return df