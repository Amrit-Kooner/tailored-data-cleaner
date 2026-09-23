# dtype_detection.py
"""
Everything about figuring out what a column actually IS -- an ID, an email,
a price, a boolean, a date -- and converting it to the right type.
"""

import re
import warnings
import pandas as pd

from config import ID_KEYWORDS, PRICE_KEYWORDS, DATE_DETECTION_THRESHOLD, PHONE_NAME_TOKENS
from utils import log_step, try_parse_date


# ---- Shared helpers ----

def name_has_keyword(col_name, keywords):
    """(unchanged from original)"""
    name = str(col_name).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", name) if t]
    squashed = re.sub(r"[^a-z0-9]+", "", name)
    for keyword in keywords:
        kw = str(keyword).lower()
        kw_tokens = [t for t in re.split(r"[^a-z0-9]+", kw) if t]
        if not kw_tokens:
            continue
        span = len(kw_tokens)
        if any(tokens[i:i + span] == kw_tokens for i in range(len(tokens) - span + 1)):
            return True
        kw_squashed = re.sub(r"[^a-z0-9]+", "", kw)
        if len(kw_squashed) >= 5 and kw_squashed in squashed:
            return True
    return False


def strip_currency_formatting(series):
    """(unchanged from original)"""
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


def to_clean_text(series):
    """(unchanged from original)"""
    def convert(value):
        if pd.isna(value):
            return pd.NA
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    return series.map(convert)


# ---- Column-type checks ----

def _sample(series):
    return series.dropna().astype(str)


def looks_like_price(col_name):
    return name_has_keyword(col_name, PRICE_KEYWORDS)


def looks_like_id(col_name, series, sample=None):
    """
    BUG FIX: a column whose NAME explicitly identifies it as a phone field
    is now short-circuited to False before the ID_KEYWORDS check. Without
    this, a "phone_number" column matched ID_KEYWORDS via the shared
    "number" token (and a bare "phone" column matched via the >95%
    uniqueness rule below), so it was classified as a text ID and the
    dedicated phone branch in infer_and_fix_dtypes was unreachable -- the
    audit log then mislabeled the column as "dtype_id".
    """
    # Short-circuit BEFORE the ID_KEYWORDS test: if the name says "phone",
    # it's a phone column, period. This has to come first because
    # "phone_number" shares the "number" token with ID_KEYWORDS.
    col_lower = str(col_name).lower()
    phone_tokens = [t for t in re.split(r"[^a-z0-9]+", col_lower) if t]
    if any(t in PHONE_NAME_TOKENS for t in phone_tokens):
        return False

    if name_has_keyword(col_name, ID_KEYWORDS):
        return True
    if looks_like_price(col_name):
        return False
    if "date" in col_name.lower() or col_name.lower().endswith("_dt"):
        return False
    if looks_like_currency(series, sample) or looks_like_percentage(series, sample):
        return False
    if looks_like_email(series, sample):
        return False
    if sample is None:
        sample = _sample(series)
    if len(sample) == 0:
        return False
    has_leading_zero = sample.str.match(r"^0\d").any()
    non_null_count = len(sample)
    uniqueness = series.nunique() / non_null_count if non_null_count > 0 else 0
    return has_leading_zero or uniqueness > 0.95


def looks_like_currency(series, sample=None):
    if sample is None:
        sample = _sample(series)
    if len(sample) == 0:
        return False
    return sample.str.contains(r"[£$€]", regex=True).mean() >= 0.3


def looks_like_percentage(series, sample=None):
    if sample is None:
        sample = _sample(series)
    if len(sample) == 0:
        return False
    return sample.str.contains(r"%", regex=False).mean() >= 0.3


def looks_like_boolean(col_name, series, sample=None):
    """
    BUG FIX: the name hint now uses WHOLE-TOKEN matching, not substring.
    Previously `any(k in name_lower for k in flag_keywords)` treated any
    column whose name merely contained "is_" / "has_" / "flag" / "active"
    as a boolean hint, so a column named "issue" (contains "is") or
    "deactivated" (contains "active") that happened to hold {yes, no} or
    {1, 0} was silently converted to boolean. The hint is now:
      * an "is_" or "has_" prefix on the name, or
      * one of the flag tokens appearing as its own token in the name.
    """
    if sample is None:
        sample = series.dropna().astype(str).str.strip().str.lower()
    else:
        sample = sample.str.strip().str.lower()
    if len(sample) == 0:
        return False

    unique_vals = set(sample.unique())
    name_lower = str(col_name).lower()

    # Unambiguous -- safe to convert with no name hint.
    if unique_vals.issubset({"true", "false"}):
        return True
    if unique_vals.issubset({"yes", "no"}):
        return True
    if unique_vals.issubset({"y", "n"}):
        return True

    # Ambiguous -- need a name hint. Match the hint as WHOLE tokens only.
    tokens = [t for t in re.split(r"[^a-z0-9]+", name_lower) if t]
    flag_tokens = {"flag", "enabled", "active", "boolean", "bool", "yn"}
    name_hint = (
        name_lower.startswith("is_")
        or name_lower.startswith("has_")
        or any(t in flag_tokens for t in tokens)
    )

    if not name_hint:
        return False

    if unique_vals.issubset({"active", "inactive"}):
        return True
    if unique_vals.issubset({"1", "0"}):
        return True
    return False


def looks_like_email(series, sample=None):
    if sample is None:
        sample = _sample(series)
    if len(sample) == 0:
        return False
    return sample.str.contains(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", regex=True).mean() >= 0.5


def looks_like_phone(col_name, series, sample=None):
    """
    BUG FIX: the name check is whole-token, not substring. Previously
    `"phone" in col_name` / `"tel" in col_name` matched any column whose
    name merely *contained* those letters -- most visibly, "hotel_name"
    was classified as a phone column because "tel" sits inside "hotel".
    That caused a hotel-name column to be rewritten as a phone-number
    column. The token match now uses the same PHONE_NAME_TOKENS list the
    cleaning.format_phone_numbers step uses, so the two are consistent.
    """
    col_lower = str(col_name).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", col_lower) if t]
    if any(t in PHONE_NAME_TOKENS for t in tokens):
        return True
    if looks_like_price(col_name):
        return False
    if sample is None:
        sample = _sample(series)
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
    """
    Detects date columns. FIX: parsed with dayfirst=True so UK-style
    dd/mm/yyyy values are recognised correctly -- without this, a UK date
    like 01/02/2026 was read as 2 January (US order) during detection and
    then written back out as 02/01/2026, silently reversing day and month.
    """
    date_cols = []
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            continue
        sample = df[col].dropna().astype(str)
        sample = sample[sample.str.strip() != ""]
        if len(sample) == 0:
            continue
        numeric_like = sample.str.match(r"^[£$€]?-?\d+(\.\d+)?%?$")
        if numeric_like.mean() >= 0.5:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            # FIX: dayfirst=True -- UK dates, e.g. 01/02/2026 == 1 Feb 2026.
            fast_parsed = pd.to_datetime(sample, errors="coerce", dayfirst=True)
        success_rate = fast_parsed.notna().sum() / len(sample)
        if success_rate < thresh:
            slow_parsed = sample.apply(try_parse_date)
            success_rate = slow_parsed.notna().sum() / len(sample)
        if success_rate >= thresh:
            date_cols.append(col)
    return date_cols


def convert_to_uk_format(df, date_cols, log, fail_mask=None):
    """
    FIX (year): writes %d/%m/%Y (four-digit year), not %y. The two-digit
    form mapped 00-68 -> 2000-2068 and 69-99 -> 1969-1999, so a legitimate
    2070-01-15 was written out as "15/01/70" and re-read as 1970-01-15.

    FIX (day/month): the fast pass now parses with dayfirst=True so a
    dd/mm/yyyy source value is not silently reversed to mm/dd/yyyy on the
    way through (e.g. 01/02/2026 no longer becomes 02/01/2026).
    """
    for col in date_cols:
        original = df[col]

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            # FIX: dayfirst=True for UK dates.
            fast_parsed = pd.to_datetime(original, errors="coerce", dayfirst=True)

        fast_parsed = pd.Series(pd.to_datetime(fast_parsed, errors="coerce"), index=original.index)

        still_missing = fast_parsed.isna() & original.notna() & (original.astype(str).str.strip() != "")
        if still_missing.any():
            slow_parsed = original.loc[still_missing].apply(try_parse_date)
            slow_parsed = pd.to_datetime(slow_parsed, errors="coerce")
            fast_parsed.loc[still_missing] = slow_parsed

        failed = fast_parsed.isna() & original.notna() & (original.astype(str).str.strip() != "")
        if failed.any():
            bad_values = original[failed].unique().tolist()
            log_step(log, "date_parse_failed",
                     f"{col}: {failed.sum()} value(s) could not be parsed and were left as their "
                     f"original (unconverted) value: {bad_values}")
            if fail_mask is not None:
                fail_mask.loc[failed[failed].index, col] = True

        # %Y (four-digit year) -- NOT %y (two-digit).
        formatted = fast_parsed.dt.strftime("%d/%m/%Y")
        df[col] = formatted.where(~failed, original)

    return df


# ---- Main dtype-fixing pass ----

def infer_and_fix_dtypes(df, log, fail_mask=None):
    """(unchanged from original)"""
    def _mark_failed(col, failed_series):
        if fail_mask is not None and failed_series.any():
            fail_mask.loc[failed_series[failed_series].index, col] = True

    for col in df.columns:
        series = df[col]
        if series.dropna().empty:
            continue
        if col in ("source_file", "missing_count"):
            continue

        sample = series.dropna().astype(str)

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
            failed = converted.isna() & series.notna()
            df[col] = converted.where(~failed, series)
            detail = f"{col} -> converted to numeric"
            if failed.any():
                bad_values = series[failed].unique().tolist()
                detail += f", {int(failed.sum())} value(s) failed to convert and were left as-is (flagged RED): {bad_values}"
            log_step(log, "dtype_currency", detail)
            _mark_failed(col, failed)
            continue

        if looks_like_percentage(series, sample):
            cleaned = series.astype(str).str.replace("%", "", regex=False).str.strip()
            converted = pd.to_numeric(cleaned, errors="coerce")
            failed = converted.isna() & series.notna()
            df[col] = converted.where(~failed, series)
            detail = f"{col} -> converted to numeric (% stripped)"
            if failed.any():
                bad_values = series[failed].unique().tolist()
                detail += f", {int(failed.sum())} value(s) failed to convert and were left as-is (flagged RED): {bad_values}"
            log_step(log, "dtype_percentage", detail)
            _mark_failed(col, failed)
            continue

        if looks_like_boolean(col, series, sample):
            bool_map = {
                "true": True, "false": False, "yes": True, "no": False,
                "y": True, "n": False, "1": True, "0": False,
                "active": True, "inactive": False,
            }
            mapped = series.astype(str).str.strip().str.lower().map(bool_map)
            failed = mapped.isna() & series.notna()
            df[col] = mapped.where(~failed, series)
            detail = f"{col} -> converted to boolean"
            if failed.any():
                bad_values = series[failed].unique().tolist()
                detail += f", {int(failed.sum())} value(s) could not be mapped and were left as-is (flagged RED): {bad_values}"
            log_step(log, "dtype_boolean", detail)
            _mark_failed(col, failed)
            continue

        if looks_like_price(col):
            converted = pd.to_numeric(strip_currency_formatting(series), errors="coerce")
            non_null = series.notna().sum()
            success_rate = converted.notna().sum() / non_null if non_null > 0 else 0
            failed = converted.isna() & series.notna()

            if success_rate >= 0.5:
                df[col] = converted.where(~failed, series)
                detail = f"{col} -> converted to numeric (price field)"
                if failed.any():
                    bad_values = series[failed].unique().tolist()
                    detail += f", {int(failed.sum())} value(s) failed to convert and were left as-is (flagged RED): {bad_values}"
                    _mark_failed(col, failed)
                log_step(log, "dtype_price_numeric", detail)
                continue
            else:
                bad_values = series[failed].unique().tolist()
                log_step(log, "dtype_price_check_failed",
                         f"{col} looks like a price column but only {success_rate:.0%} of values "
                         f"converted -- left as text, nothing changed. Bad value(s): {bad_values}")

        if looks_like_category(series):
            df[col] = series.astype("category")
            log_step(log, "dtype_category", f"{col} -> converted to category ({series.nunique()} unique values)")
            continue

        df[col] = to_clean_text(series)
        log_step(log, "dtype_text", f"{col} -> left as free text")

    return df