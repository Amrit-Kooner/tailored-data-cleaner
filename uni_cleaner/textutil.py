# textutil.py
"""
Column-name and text helpers shared by several modules. Before this file
existed, the same name-splitting regex was copy-pasted into five places.
"""

import numbers
import re
import pandas as pd

import config


def normalize_name(name):
    """'Part Number ' -> 'part_number' (the pipeline's canonical column name)."""
    return str(name).strip().lower().replace(" ", "_")


def name_tokens(name):
    """'phone_number' / 'Phone-Number' -> ['phone', 'number']"""
    return [t for t in re.split(r"[^a-z0-9]+", str(name).lower()) if t]


def name_has_keyword(col_name, keywords):
    """(unchanged from original)"""
    name = str(col_name).lower()
    tokens = name_tokens(name)
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


def name_has_word(col_name, keywords):
    """
    WHOLE-WORD match: True when a keyword appears in the column name as its
    own word(s) -- never as part of a longer word.

        "price" / "unit_price" / "list_price" / "prices" / "unitprice" -> match "price"/"unit_price"
        "pricelist" / "priceband" / "discharge_date" / "valued_by"     -> NO match

    Unlike name_has_keyword() there is no substring fallback (that is what let
    "discharge" match "charge"). A plural ("prices", "amounts") counts, and a
    multi-word keyword also matches when written as one word ("unit_price" ->
    "unitprice"), because that's a common spreadsheet header style.
    """
    tokens = name_tokens(col_name)
    if not tokens:
        return False
    for keyword in keywords:
        kw = name_tokens(keyword)
        if not kw:
            continue
        span = len(kw)
        for i in range(len(tokens) - span + 1):
            window = tokens[i:i + span]
            if window == kw or (window[:-1] == kw[:-1] and window[-1] == kw[-1] + "s"):
                return True
        if span > 1 and "".join(kw) in tokens:
            return True
    return False


def name_is_keyword(col_name, keywords):
    """
    EXACT column-name match: True only when the whole name IS one of the
    keywords (ignoring case, spaces, underscores, punctuation).

        "app" / "App No." / "APP_NUMBER" / "appno"  -> match  (keyword "app_no" etc.)
        "app_name" / "app_description" / "bpa_date" -> NO match
        "app.1" / "app_number_alt_1"                -> NO match (extra copies of a column)

    name_has_keyword() is a loose "contains this word" test. That's right for
    guessing a column's TYPE, but far too loose for deciding which column is
    THE app number: it made "app_name" count as an app number, which
    (a) merged rows with different app numbers in dedupe_by_app_number and
    (b) painted ordinary repeats BLUE in flag_id_anomalies.
    """
    tokens = name_tokens(col_name)
    if not tokens:
        return False
    squashed = "".join(tokens)
    for keyword in keywords:
        kw_tokens = name_tokens(keyword)
        if not kw_tokens:
            continue
        if tokens == kw_tokens or squashed == "".join(kw_tokens):
            return True
    return False


def col_name_matches_keyword(col_name, keyword):
    """
    Whole-token / word-boundary match for column-name keywords.

    BUG FIX (kept from the original): standardize_casing used to do a plain
    substring check, so a column named "username" matched the "name" rule and
    had every value title-cased -- silently corrupting case-sensitive
    identifiers ("JohnDoe123" -> "Johndoe123").

    The keyword must sit on a word boundary: preceded by start-of-string or a
    non-LETTER character, and followed by end-of-string or a non-LETTER
    character. Digits count as boundaries on purpose, so pandas' dedup-suffixed
    columns like "name2" and "email2" still match "name"/"email"; letters do
    not:
        "user_name"  -> matches "name"   (underscore boundary)
        "name2"      -> matches "name"   (digit boundary)
        "username"   -> does NOT match "name"   (letters before/after)
    """
    name = str(col_name).lower()
    kw = str(keyword).lower()
    if not kw:
        return False
    pattern = rf"(^|[^a-z]){re.escape(kw)}([^a-z]|$)"
    return re.search(pattern, name) is not None


def is_unnamed_column(col):
    """
    True for a column header that pandas invented because the source file's
    header cell was blank (e.g. "Unnamed: 3") or literally missing (NaN).
    Shared by flag_problem_columns (logging) and flag_unnamed_columns
    (BLUE header flag) so both use exactly the same definition.
    """
    return pd.isna(col) or str(col).startswith("Unnamed")


def _is_bool_value(value):
    """Python bool or numpy bool_. Never True for int 0/1."""
    return isinstance(value, bool) or type(value).__name__ == "bool_"


def casefold_for_compare(value):
    """
    Canonical comparison key used to decide whether two cells hold "the same
    value" (case-insensitive for text). Returns a STRING sentinel so a Series
    of them always compares element-wise with `==` -- a Series of same-length
    tuples can get collapsed to a 2-D object array by pandas/numpy, at which
    point `a == b` broadcasts and every cell comes back "equal". Strings
    cannot hit that.

    Semantics guaranteed by this function:
        NaN, None, pd.NA, NaT             -> __NULL__          (one value)
        True / False (bool, numpy.bool_)  -> __BOOL__True/False
        0 / 0.0 / -0.0                    -> __NUM__0          (one value)
        1 / 1.0                           -> __NUM__1
        "ACME" / "acme" / " Acme "        -> __STR__acme        (one value)
        "5"                               -> __STR__5   (NOT __NUM__5)

    0, False and a blank are three DIFFERENT keys. Never equal to each other.
    """
    if pd.isna(value):
        return "__NULL__"
    if _is_bool_value(value):
        return "__BOOL__True" if value else "__BOOL__False"
    if isinstance(value, str):
        return "__STR__" + value.strip().lower()
    if isinstance(value, numbers.Number):
        try:
            as_float = float(value)
            if as_float.is_integer():
                return "__NUM__" + str(int(as_float))
            return "__NUM__" + repr(as_float)
        except (TypeError, ValueError, OverflowError):
            return "__NUM__" + repr(value)
    return "__OBJ__" + type(value).__name__ + ":" + repr(value)


def is_text_dtype(series):
    """True for object / pandas-3 'str' / 'string' columns."""
    return str(series.dtype) in ("object", "str", "string")


def safe_changed_mask(before, after):
    """
    Boolean "this value was rewritten" mask that is guaranteed never to
    contain pd.NA, whatever the input dtype. (`fillna(False)` is a no-op on a
    plain numpy bool Series, so it is safe to apply unconditionally.)
    """
    diff = (before != after)
    diff = diff.fillna(False).astype(bool)
    return (before.notna() & diff).astype(bool)


def to_clean_text(series):
    """(unchanged from original)"""
    def convert(value):
        if pd.isna(value):
            return pd.NA
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    return series.map(convert)


_disguised_blank_values_cache = {"source": None, "values": None}


def disguised_blank_values():
    """
    config.DISGUISED_BLANKS as a lower-case, trimmed set (matching ignores
    case and spaces).

    Cached, keyed on the identity of config.DISGUISED_BLANKS itself (not just
    computed once globally): this set used to be rebuilt from the ~15-item
    config list on EVERY call, including once per CELL wherever
    is_disguised_blank() runs inside a per-cell .map()/loop -- e.g. tens of
    thousands of rebuilds for one 22k-row column. Keying on identity (rather
    than a plain lru_cache) means the cache still rebuilds correctly if
    config.DISGUISED_BLANKS is ever swapped for a different list object (as
    tests do with mock.patch.object), while a normal run builds the set once
    and reuses it for every remaining call.
    """
    cache = _disguised_blank_values_cache
    current = config.DISGUISED_BLANKS
    if cache["source"] is not current:
        cache["source"] = current
        cache["values"] = {str(v).strip().lower() for v in current}
    return cache["values"]


def is_disguised_blank(value):
    """True for a cell that holds placeholder text ('N/A', 'TBD', '-', ...). A real blank (NaN) is not one."""
    return isinstance(value, str) and value.strip().lower() in disguised_blank_values()


def disguised_blank_mask(series):
    """Boolean Series, True where a cell holds placeholder text. Non-text columns are never flagged."""
    if str(series.dtype) in ("object", "str", "string", "category"):
        placeholders = disguised_blank_values()
        found = series.astype(object).map(
            lambda v: isinstance(v, str) and v.strip().lower() in placeholders
        )
        return found.astype(bool)
    return pd.Series(False, index=series.index)