# steps/values.py
"""
Steps that rewrite cell VALUES without changing the table's shape: trimming,
encoding repair, phone formatting, casing, canonical
category values. Every step is  step(df, ctx) -> df, and every one is
wrapped in @value_only so a populated value can never silently vanish.

Every per-cell step here skips ctx.blank_cols -- fully blank columns carry
no data to clean, and walking their 22k NaN cells was a large fraction of
run time on wide sparse files.
"""

import re
import unicodedata

import pandas as pd

from config import (
    CATEGORY_MAPPINGS, CASING_RULES,
    PHONE_EXACT_NAMES, PHONE_MIN_LIKE_RATE,
)
from ..audit import log_step
from ..detectors import name_has_phone_token
from ..guards import value_only
from ..textutil import col_name_matches_keyword, is_text_dtype, safe_changed_mask


@value_only()
def trim_whitespace(df, ctx):
    """
    Strips leading/trailing whitespace from string VALUES (headers are handled elsewhere).

    Fully blank columns are skipped (ctx.blank_cols) -- there is no text to
    trim, and mapping over their NaN cells was wasted work.
    """
    stats = ctx.stats
    stats["values_trimmed"] = 0
    trimmed_cols = []

    def strip_and_count(value):
        if isinstance(value, str):
            stripped = value.strip()
            if stripped != value:
                stats["values_trimmed"] += 1
                return stripped
        return value

    for col in df.select_dtypes(include=["object", "str"]).columns:
        if col in ctx.blank_cols:
            continue
        before_count = stats["values_trimmed"]
        df[col] = df[col].map(strip_and_count)
        if stats["values_trimmed"] > before_count:
            trimmed_cols.append(col)
    log_step(ctx.log, "trim_whitespace",
             f"{stats['values_trimmed']} value(s) had leading/trailing whitespace removed"
             + (f" in columns: {trimmed_cols}" if trimmed_cols else "")
             + f" ({len(ctx.blank_cols)} blank column(s) skipped)")
    return df


@value_only()
def fix_encoding_issues(df, ctx):
    """
    Repairs mojibake and swaps smart quotes / nbsp / dashes for plain ASCII.

    Fully blank columns are skipped (ctx.blank_cols).
    """
    replacements = {
        "\ufeff": "",
        "\u00a0": " ",
        "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-",
        "\u2026": "...",
    }

    def repair_mojibake(value):
        try:
            repaired = value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value
        return repaired

    def normalize_value(value):
        if not isinstance(value, str):
            return value
        if value.isascii():
            return value
        value = repair_mojibake(value)
        for bad, good in replacements.items():
            value = value.replace(bad, good)
        return unicodedata.normalize("NFKC", value)

    affected_cols = []
    total_changed = 0
    for col in df.select_dtypes(include=["object", "str"]).columns:
        if col in ctx.blank_cols:
            continue
        before = df[col]
        is_str = before.map(lambda v: isinstance(v, str))
        non_ascii = before.map(lambda v: isinstance(v, str) and not v.isascii())
        if not non_ascii.any():
            continue
        cleaned = before.copy()
        cleaned.loc[non_ascii] = before[non_ascii].map(normalize_value)
        changed_mask = safe_changed_mask(before, cleaned) & is_str
        changed_count = int(changed_mask.sum())
        if changed_count:
            affected_cols.append(col)
            total_changed += changed_count
        df[col] = cleaned
    detail = f"{total_changed} value(s) normalized"
    detail += f" across columns: {affected_cols}" if affected_cols else " (no special characters found)"
    detail += f" ({len(ctx.blank_cols)} blank column(s) skipped)"
    log_step(ctx.log, "fix_encoding_issues", detail)
    return df


_PHONE_VALUE_RE = re.compile(r"^[\s()+\-.\d]{5,}$")


def _to_digits_only(value):
    """UK-flavoured phone normalisation; values that aren't phone-shaped pass through."""
    if pd.isna(value):
        return value
    s = str(value).strip()
    if not _PHONE_VALUE_RE.match(s):
        return value
    digits = re.sub(r"\D", "", s)
    if digits.startswith("0044"):
        digits = digits[2:]
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    elif digits.startswith("7") and len(digits) == 10:
        digits = "0" + digits
    return digits


@value_only()
def format_phone_numbers(df, ctx):
    """
    Digits-only phone numbers -- but only in columns whose NAME says phone
    and whose values actually look phone-like. Fully blank columns are
    skipped (they can't hold phone numbers).
    """
    log = ctx.log
    any_candidate = False
    for col in df.columns:
        if col in ctx.blank_cols:
            continue
        is_named_phone = name_has_phone_token(col)
        is_bare_number = str(col).lower() in PHONE_EXACT_NAMES
        if not (is_named_phone or is_bare_number):
            continue
        any_candidate = True
        if not is_text_dtype(df[col]):
            continue

        before = df[col]
        non_null = before.dropna()
        if len(non_null) == 0:
            log_step(log, "format_phone_numbers",
                     f"{col}: skipped -- column is entirely blank, nothing to standardize")
            continue

        phone_like_rate = non_null.map(
            lambda v: bool(_PHONE_VALUE_RE.match(str(v).strip()))
        ).mean()

        if phone_like_rate < PHONE_MIN_LIKE_RATE:
            log_step(log, "format_phone_numbers",
                     f"{col}: skipped -- only {phone_like_rate:.0%} of values look phone-like, "
                     f"left untouched to avoid destroying non-phone data")
            continue

        after = before.map(_to_digits_only)
        changed_mask = safe_changed_mask(before, after)
        changed_count = int(changed_mask.sum())
        df[col] = after
        if changed_count:
            log_step(log, "format_phone_numbers",
                     f"{col}: {changed_count} value(s) standardized to digits-only")
        else:
            log_step(log, "format_phone_numbers",
                     f"{col}: 0 value(s) changed (already standardized)")
    if not any_candidate:
        log_step(log, "format_phone_numbers",
                 "skipped -- no phone-like column name found")
    return df


# ---------------------------------------------------------------------------
# COMMENTED OUT FOR NOW: turning disguised blanks into real blanks.
#
# Placeholder text ('N/A', 'TBD', 'unknown', '-' ...) is no longer removed. It is
# kept and every cell holding one is flagged PURPLE instead
# (masks.build_disguised_mask, called at the end of pipeline.clean_file; the
# placeholder words are config.DISGUISED_BLANKS). To bring the removal back:
# uncomment this function, put it back in pipeline.STRUCTURE_STEPS, put
# `DISGUISED_BLANKS` back in the `from config import (...)` above, and stop pandas
# blanking them at load time (loaders._na_values_to_read).
#
# @value_only(allow_blanks=DISGUISED_BLANKS)
# def convert_disguised_blanks(df, ctx):
#     """'N/A', 'TBD', 'unknown', '-' ... -> real missing values (the ONE intentional value loss)."""
#     stats = ctx.stats
#     before_na = df.isna()
#     df = df.replace(DISGUISED_BLANKS, pd.NA)
#     newly_blanked = (df.isna() & ~before_na).sum()
#     newly_blanked = newly_blanked[newly_blanked > 0]
#     stats["disguised_blanks_converted"] = int(newly_blanked.sum())
#     detail = (f"{stats['disguised_blanks_converted']} disguised blank value(s) "
#               f"(e.g. 'N/A', 'unknown', 'TBD') converted to real missing values")
#     if not newly_blanked.empty:
#         detail += f" -- by column: {newly_blanked.to_dict()}"
#     log_step(ctx.log, "convert_disguised_blanks", detail)
#     return df
# ---------------------------------------------------------------------------


def summarize_missing_values(df, ctx):
    """Log-only step: how many rows have at least one missing field."""
    missing_count = df.isna().sum(axis=1)
    rows_with_missing = int((missing_count > 0).sum())
    log_step(ctx.log, "missing_value_summary",
             f"{rows_with_missing} row(s) contain at least one missing field after cleaning")
    return df


@value_only()
def standardize_casing(df, ctx):
    """
    Applies the configured per-column casing rule (lower / title / upper)
    when the column NAME contains a matching keyword.

    Fully blank columns are skipped (ctx.blank_cols).
    """
    matched_any = False
    for col in df.columns:
        if col in ctx.blank_cols:
            continue
        col_lower = str(col).lower()
        for keyword, rule in CASING_RULES.items():
            if col_name_matches_keyword(col_lower, keyword) and is_text_dtype(df[col]):
                matched_any = True
                before = df[col]
                if rule == "lower":
                    after = before.map(lambda v: v.lower() if isinstance(v, str) else v)
                elif rule == "title":
                    after = before.map(lambda v: v.title() if isinstance(v, str) else v)
                elif rule == "upper":
                    after = before.map(lambda v: v.upper() if isinstance(v, str) else v)
                else:
                    after = before

                changed_mask = safe_changed_mask(before, after)
                changed_count = int(changed_mask.sum())
                df[col] = after
                if changed_count:
                    examples = list(before[changed_mask].unique()[:3])
                    log_step(ctx.log, "standardize_casing",
                             f"{col}: {changed_count} value(s) converted to {rule} case (e.g. {examples})")
                else:
                    log_step(ctx.log, "standardize_casing",
                             f"{col}: 0 value(s) changed (already {rule} case)")
                break
    if not matched_any:
        log_step(ctx.log, "standardize_casing",
                 f"skipped -- no column name matched a casing rule ({', '.join(CASING_RULES)})")
    return df


@value_only()
def apply_category_mapping(df, ctx, mappings=CATEGORY_MAPPINGS):
    """
    Maps messy category values onto the canonical ones defined in config.CATEGORY_MAPPINGS.
    Fully blank columns are skipped (ctx.blank_cols).
    """
    matched_any = False
    for col, mapping in mappings.items():
        if col in df.columns and col not in ctx.blank_cols:
            matched_any = True
            lookup = {str(k).strip().lower(): v for k, v in mapping.items()}

            def resolve(v):
                if pd.isna(v):
                    return v
                return lookup.get(str(v).strip().lower(), v)

            before = df[col]
            after = before.map(resolve)
            changed_mask = safe_changed_mask(before, after)
            changed_count = int(changed_mask.sum())
            df[col] = after
            if changed_count:
                changed_from = sorted(set(before[changed_mask].unique().tolist()))
                log_step(ctx.log, "category_value_mapping",
                         f"{col}: {changed_count} value(s) mapped {changed_from} -> canonical values")
            else:
                log_step(ctx.log, "category_value_mapping",
                         f"{col}: 0 value(s) needed mapping (already canonical)")
    if not matched_any:
        log_step(ctx.log, "category_value_mapping",
                 f"skipped -- no column named {list(mappings.keys())} found")
    return df