# steps/dates.py
"""
Date detection and conversion to UK dd/mm/yyyy.

Fully blank columns are skipped by ctx.blank_cols in detect_date_columns --
they can never contain a date, and running pd.to_datetime on 22k NaN cells
per column was wasted work on wide sparse files.
"""

import re
import warnings

import pandas as pd
from dateutil import parser as dateparser

from config import DATE_DETECTION_THRESHOLD
from ..audit import log_step
from ..guards import value_only
from ..masks import mark_failed
from ..textutil import disguised_blank_mask


# Year-first dates (2026-04-01, 2026/04/01, 2026-04-01 13:45:00) are unambiguous:
# they are always year-month-day. They must NOT be parsed with dayfirst=True --
# pandas and dateutil both honour that flag on them and silently swap day and
# month (2026-04-01 -> 4 January), which is wrong whenever the day is 12 or less.
_YEAR_FIRST_RE = re.compile(r"^\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[T\s].*)?$")


def _is_year_first(text):
    # pandas 3 keeps missing values as NaN (a float) even after astype(str)
    return isinstance(text, str) and bool(_YEAR_FIRST_RE.match(text))


def parse_dates_uk(series):
    """
    pd.to_datetime(errors="coerce") with UK day-first order for dd/mm/yyyy
    style text, except year-first text which is always year-month-day.
    Returns a datetime Series with the same index; unparseable -> NaT.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, errors="coerce")

    positional = series.reset_index(drop=True)
    as_text = positional.astype(str)
    year_first = positional.notna() & as_text.map(_is_year_first)
    other = positional.notna() & ~year_first

    pieces = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        if year_first.any():
            pieces.append(pd.to_datetime(positional[year_first], errors="coerce", dayfirst=False))
        if other.any():
            pieces.append(pd.to_datetime(positional[other], errors="coerce", dayfirst=True))
    if not pieces:
        parsed = pd.Series(pd.NaT, index=positional.index, dtype="datetime64[ns]")
    else:
        parsed = pd.to_datetime(pd.concat(pieces), errors="coerce").reindex(positional.index)
    parsed.index = series.index
    return parsed


def try_parse_date(value):
    """
    Tries to parse a single value as a date. Returns a datetime, or None if
    it can't be parsed (blank, empty string, or a genuinely invalid date like
    "2023-13-40"). Used as the slow fallback path below -- calling this
    per-cell across a whole column is the slowest part of the pipeline, so
    it's only reached when the fast vectorized pass isn't confident enough.
    """
    if pd.isna(value) or str(value).strip() == "":
        return None
    try:
        text = str(value)
        return dateparser.parse(text, dayfirst=not _is_year_first(text), fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None


def detect_date_columns(df, ctx, thresh=DATE_DETECTION_THRESHOLD):
    """
    Detects date columns. Fully blank columns are skipped (ctx.blank_cols) --
    they cannot contain a date, and running pd.to_datetime on 22k NaN cells
    per column is a big waste on wide sparse files.

    Parsed with dayfirst=True so UK-style dd/mm/yyyy values are recognised
    correctly -- without it a UK date like 01/02/2026 was read as 2 January
    (US order) during detection and then written back out as 02/01/2026,
    silently reversing day and month.
    """
    date_cols = []
    for col in df.columns:
        if col in ctx.blank_cols:
            continue
        if pd.api.types.is_numeric_dtype(df[col]):
            continue
        sample = df[col][df[col].notna() & ~disguised_blank_mask(df[col])].astype(str)
        sample = sample[sample.str.strip() != ""]
        if len(sample) == 0:
            continue
        numeric_like = sample.str.match(r"^[£$€]?-?\d+(\.\d+)?%?$")
        if numeric_like.mean() >= 0.5:
            continue
        fast_parsed = parse_dates_uk(sample)
        success_rate = fast_parsed.notna().sum() / len(sample)
        if success_rate < thresh:
            slow_parsed = sample.apply(try_parse_date)
            success_rate = slow_parsed.notna().sum() / len(sample)
        if success_rate >= thresh:
            date_cols.append(col)
    return date_cols


def convert_to_uk_format(df, date_cols, log, fail_mask=None):
    """
    Writes %d/%m/%Y (four-digit year), not %y -- the two-digit form mapped
    00-68 -> 2000-2068 and 69-99 -> 1969-1999, so a legitimate 2070-01-15 was
    written out as "15/01/70" and re-read as 1970-01-15. Parsing uses
    dayfirst=True so dd/mm/yyyy is not silently reversed. Values that cannot
    be parsed are left as their original text and flagged RED.
    """
    for col in date_cols:
        original = df[col]

        fast_parsed = pd.Series(parse_dates_uk(original), index=original.index)

        still_missing = fast_parsed.isna() & original.notna() & (original.astype(str).str.strip() != "")
        if still_missing.any():
            slow_parsed = original.loc[still_missing].apply(try_parse_date)
            slow_parsed = pd.to_datetime(slow_parsed, errors="coerce")
            fast_parsed.loc[still_missing] = slow_parsed

        # Placeholder text ('N/A', 'TBD', ...) is not a failed date: it is kept as-is and flagged PURPLE later.
        placeholder = disguised_blank_mask(original)
        failed = fast_parsed.isna() & original.notna() & (original.astype(str).str.strip() != "") & ~placeholder
        if failed.any():
            bad_values = original[failed].unique().tolist()
            log_step(log, "date_parse_failed",
                     f"{col}: {failed.sum()} value(s) could not be parsed and were left as their "
                     f"original (unconverted) value: {bad_values}")
            mark_failed(fail_mask, col, failed)

        formatted = fast_parsed.dt.strftime("%d/%m/%Y")
        df[col] = formatted.where(~(failed | placeholder), original)

    return df


@value_only()
def convert_dates(df, ctx):
    """Pipeline step: detect date columns (-> ctx.date_cols) and convert them to dd/mm/yyyy."""
    ctx.date_cols = detect_date_columns(df, ctx)
    df = convert_to_uk_format(df, ctx.date_cols, ctx.log, ctx.fail_mask)
    log_step(ctx.log, "format_dates",
             f"{len(ctx.date_cols)} date column(s) converted to dd/mm/yyyy: {ctx.date_cols}")
    return df