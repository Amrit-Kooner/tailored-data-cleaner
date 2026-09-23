"""
Everything about figuring out what a column actually IS -- an ID, an email,
a price, a boolean, a date -- and converting it to the right type. This is
the "smart guessing" layer of the pipeline. Update PRICE_KEYWORDS/ID_KEYWORDS
in config.py to adjust detection without touching any logic here.
"""

import re
import warnings
import pandas as pd

from config import ID_KEYWORDS, PRICE_KEYWORDS, DATE_DETECTION_THRESHOLD
from utils import log_step, try_parse_date


# ---- Column-type checks ----

def looks_like_price(col_name):
    # Splits the column name into tokens on any non-alphanumeric character
    # (underscore, space, dash, etc.) and checks whether a keyword equals a
    # WHOLE token -- not a substring match. This means "rate" won't falsely
    # match inside "corporate", and "fee" won't match inside "coffee", while
    # "total_price" still correctly matches (splits into "total" + "price").
    tokens = re.split(r"[^a-z0-9]+", col_name.lower())
    return any(k in tokens for k in PRICE_KEYWORDS)


def looks_like_id(col_name, series):
    if any(k in col_name.lower() for k in ID_KEYWORDS):
        return True

    # A price column is often highly unique per row too (every transaction has a
    # different amount) -- don't let that fool the uniqueness heuristic below.
    if looks_like_price(col_name):
        return False

    # Same problem applies to dates: once formatted, every value in a date
    # column is usually unique too.
    if "date" in col_name.lower() or col_name.lower().endswith("_dt"):
        return False

    sample = series.dropna().astype(str)
    if len(sample) == 0:
        return False

    has_leading_zero = sample.str.match(r"^0\d").any()
    uniqueness = series.nunique() / len(series.dropna()) if len(series.dropna()) > 0 else 0
    return has_leading_zero or uniqueness > 0.95


def looks_like_currency(series):
    sample = series.dropna().astype(str)
    if len(sample) == 0:
        return False
    return sample.str.contains(r"[£$€]", regex=True).mean() >= 0.3


def looks_like_percentage(series):
    sample = series.dropna().astype(str)
    if len(sample) == 0:
        return False
    return sample.str.contains(r"%", regex=False).mean() >= 0.3


def looks_like_boolean(col_name, series):
    sample = series.dropna().astype(str).str.strip().str.lower()
    if len(sample) == 0:
        return False
    text_boolean_sets = [{"true", "false"}, {"yes", "no"}, {"y", "n"}, {"active", "inactive"}]
    unique_vals = set(sample.unique())
    if any(unique_vals.issubset(b) for b in text_boolean_sets):
        return True
    flag_keywords = ["is_", "has_", "flag", "active", "enabled", "status"]
    if unique_vals.issubset({"1", "0"}) and any(k in col_name.lower() for k in flag_keywords):
        return True
    return False


def looks_like_email(series):
    sample = series.dropna().astype(str)
    if len(sample) == 0:
        return False
    return sample.str.contains(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", regex=True).mean() >= 0.5


def looks_like_phone(col_name, series):
    if "phone" in col_name.lower() or "tel" in col_name.lower():
        return True
    sample = series.dropna().astype(str)
    if len(sample) == 0:
        return False
    pattern = r"^[\d\s\-\(\)\.\+]{7,}$"
    return sample.str.match(pattern).mean() >= 0.5


def looks_like_category(series, max_unique_ratio=0.2):
    non_null = series.dropna()
    if len(non_null) == 0:
        return False
    unique_ratio = non_null.nunique() / len(non_null)
    return unique_ratio <= max_unique_ratio and non_null.nunique() > 1


# ---- Date detection / conversion ----

def detect_date_columns(df, thresh=DATE_DETECTION_THRESHOLD):
    date_cols = []
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            continue

        sample = df[col].dropna().astype(str)
        sample = sample[sample.str.strip() != ""]
        if len(sample) == 0:
            continue

        # Skip columns that look like plain numbers/currency -- bare numbers
        # can otherwise be misread as dates by the parser.
        numeric_like = sample.str.match(r"^[£$€]?-?\d+(\.\d+)?%?$")
        if numeric_like.mean() >= 0.5:
            continue

        # Fast vectorized pass first. If a column has ONE consistent date
        # format, this alone can be near-instant even on huge columns. Mixed
        # formats make pandas fall back to slow per-element parsing anyway --
        # the warning that produces is expected and suppressed here, since
        # the slow fallback below handles whatever it can't confidently parse.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            fast_parsed = pd.to_datetime(sample, errors="coerce", dayfirst=False)
        success_rate = fast_parsed.notna().sum() / len(sample)

        if success_rate < thresh:
            slow_parsed = sample.apply(try_parse_date)
            success_rate = slow_parsed.notna().sum() / len(sample)

        if success_rate >= thresh:
            date_cols.append(col)

    return date_cols


def convert_to_uk_format(df, date_cols, log):
    for col in date_cols:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            fast_parsed = pd.to_datetime(df[col], errors="coerce", dayfirst=False)

        still_missing = fast_parsed.isna() & df[col].notna() & (df[col].astype(str).str.strip() != "")
        if still_missing.any():
            slow_parsed = df.loc[still_missing, col].apply(try_parse_date)
            fast_parsed.loc[still_missing] = slow_parsed

        failed = fast_parsed.isna() & df[col].notna() & (df[col].astype(str).str.strip() != "")
        if failed.any():
            log_step(log, "date_parse_failed", f"{col}: {failed.sum()} value(s) could not be parsed and were left blank")

        df[col] = fast_parsed.dt.strftime("%d/%m/%y")

    return df


# ---- Main dtype-fixing pass ----

def infer_and_fix_dtypes(df, log):
    """
    Walks every column and converts it to what its values actually look
    like. Checked most-specific-first, so an ID or currency column doesn't
    accidentally get treated as plain free text. Run this LAST in the
    pipeline, once whitespace/casing/blank-handling have already made the
    values genuinely clean.
    """
    for col in df.columns:
        series = df[col]
        if series.dropna().empty:
            continue
        if col in ("source_file", "missing_count"):
            continue

        if looks_like_id(col, series):
            df[col] = series.astype(str)
            log_step(log, "dtype_id", f"{col} -> treated as text ID")
            continue

        if looks_like_email(series):
            df[col] = series.astype(str).str.lower()
            log_step(log, "dtype_email", f"{col} -> treated as text email")
            continue

        if looks_like_phone(col, series):
            df[col] = series.astype(str)
            log_step(log, "dtype_phone", f"{col} -> treated as text phone number")
            continue

        if looks_like_currency(series):
            cleaned = (series.astype(str)
                       .str.replace(r"[£$€]", "", regex=True)
                       .str.replace(",", "", regex=False)
                       .str.strip())
            converted = pd.to_numeric(cleaned, errors="coerce")
            failed = converted.isna() & series.notna()
            df[col] = converted
            log_step(log, "dtype_currency", f"{col} -> converted to numeric, {failed.sum()} value(s) failed to convert")
            continue

        if looks_like_percentage(series):
            cleaned = series.astype(str).str.replace("%", "", regex=False).str.strip()
            df[col] = pd.to_numeric(cleaned, errors="coerce")
            log_step(log, "dtype_percentage", f"{col} -> converted to numeric (% stripped)")
            continue

        if looks_like_boolean(col, series):
            bool_map = {
                "true": True, "false": False, "yes": True, "no": False,
                "y": True, "n": False, "1": True, "0": False,
                "active": True, "inactive": False,
            }
            df[col] = series.astype(str).str.strip().str.lower().map(bool_map)
            log_step(log, "dtype_boolean", f"{col} -> converted to boolean")
            continue

        if looks_like_price(col):
            converted = pd.to_numeric(series, errors="coerce")
            non_null = series.notna().sum()
            success_rate = converted.notna().sum() / non_null if non_null > 0 else 0
            if success_rate >= 0.9:
                df[col] = converted
                log_step(log, "dtype_price_numeric", f"{col} -> converted to numeric (price field)")
                continue
            else:
                bad_values = series[converted.isna() & series.notna()].unique().tolist()
                log_step(log, "dtype_price_check_failed",
                         f"{col} looks like a price column but only {success_rate:.0%} of values "
                         f"converted -- left as text. Bad value(s): {bad_values}")

        if looks_like_category(series):
            df[col] = series.astype("category")
            log_step(log, "dtype_category", f"{col} -> converted to category ({series.nunique()} unique values)")
            continue

        df[col] = series.astype(str)
        log_step(log, "dtype_text", f"{col} -> left as free text")

    return df
