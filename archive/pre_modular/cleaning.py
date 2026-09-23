# cleaning.py
"""
The "make the values consistent" steps -- casing, canonical category values,
duplicate-named columns, encoding artifacts, and finding the real header row.
"""

import re
import unicodedata
import pandas as pd

from config import (
    CASING_RULES, DUPLICATE_COLUMN_MATCH_THRESHOLD, HEADER_DETECTION_THRESHOLD,
    PHONE_NAME_TOKENS, PHONE_EXACT_NAMES, PHONE_MIN_LIKE_RATE,
)
from utils import log_step


# Values that look like a phone number: digits, spaces, and the usual
# separators, at least 5 characters long. Deliberately narrow so that
# free text ("call office"), reference codes ("PART-001"), and disguised
# blanks ("-", "N/A") never match.
_PHONE_VALUE_RE = re.compile(r"^[\s()+\-.\d]{5,}$")


def _col_name_matches_keyword(col_name, keyword):
    """
    Whole-token / word-boundary match for column-name keywords.

    BUG FIX: standardize_casing used to do a plain substring check
    (`keyword in col_lower`), so a column named "username" matched the
    "name" rule and had every value title-cased -- silently corrupting
    case-sensitive identifiers like usernames ("JohnDoe123" -> "Johndoe123").

    The keyword must sit on a word boundary: preceded by start-of-string
    or a non-LETTER character, and followed by end-of-string or a
    non-LETTER character. Digits count as boundaries on purpose, so
    pandas' dedup-suffixed columns like "name2" and "email2" still match
    "name"/"email"; letters do not. This means:
        "user_name"  -> matches "name"   (underscore boundary)
        "name_field" -> matches "name"   (underscore boundary)
        "name2"      -> matches "name"   (digit boundary)
        "username"   -> does NOT match "name"   (letters before/after)
        "myemail"    -> does NOT match "email"  (letters before)
    """
    name = str(col_name).lower()
    kw = str(keyword).lower()
    if not kw:
        return False
    pattern = rf"(^|[^a-z]){re.escape(kw)}([^a-z]|$)"
    return re.search(pattern, name) is not None


def _safe_changed_mask(before, after):
    """
    BUG FIX: builds a boolean "this value was rewritten" mask that is
    guaranteed never to contain pd.NA, regardless of the input dtype.

    The previous version only filled NA when the comparison result was
    object dtype, which relied on Kleene logic (`False & NA == False`)
    to save it whenever a nullable-boolean comparison was involved --
    fragile, and it would raise on the first Series where before.notna()
    was True alongside a NA diff. `fillna(False)` is a no-op on a plain
    numpy bool Series, so this is safe to apply unconditionally.
    """
    diff = (before != after)
    diff = diff.fillna(False).astype(bool)
    return (before.notna() & diff).astype(bool)


def _looks_like_header_cell(value):
    """
    A header cell is short, non-empty, and not purely numeric. Used by
    find_header_row to avoid eating a dense first DATA row as the header
    just because it happens to be >50% populated.
    """
    s = str(value).strip()
    if s == "":
        return False
    # Plain number (allow common currency/percent decorations around it)
    candidate = re.sub(r"[£$€¥%,\s]", "", s)
    try:
        float(candidate)
        return False
    except (ValueError, TypeError):
        return True


def find_header_row(raw_df, thresh=HEADER_DETECTION_THRESHOLD):
    """
    Finds the row index where the real header lives, skipping blank/junk
    rows above it. A row counts as "the header" once at least `thresh`
    fraction of its cells are filled in AND the majority of those filled
    cells look like header text (short, non-numeric) rather than data.

    That second condition is what stops a dense first DATA row (e.g. a
    headerless export whose first record happens to have most cells filled)
    from being eaten as the header and silently dropped from the output.
    """
    if len(raw_df) == 0:
        return 0
    filled = raw_df.notna().sum(axis=1) / raw_df.shape[1]
    hits = filled[filled >= thresh]
    if not len(hits):
        return 0

    for idx in hits.index:
        row = raw_df.loc[idx]
        populated = [v for v in row if not pd.isna(v) and str(v).strip() != ""]
        if not populated:
            continue
        texty = sum(_looks_like_header_cell(v) for v in populated) / len(populated)
        if texty >= 0.7:
            return int(idx)

    # No row satisfied the "header-texty" test -- fall back to the old
    # fill-ratio-only behaviour so we don't break files that legitimately
    # have numeric-looking headers.
    return int(hits.index[0])


def remove_repeated_header_rows(df, log):
    """
    Removes rows whose values are identical to the column headers, i.e.
    a header that got repeated mid-file.

    IMPORTANT: a legitimate data row can be identical to the headers (e.g.
    a record whose ID literally reads "ID", name "NAME", amount "AMOUNT").
    Removing those would be silent data loss. This function is therefore
    deliberately conservative:

      * A single comparable column is never enough -- a one-column "match"
        is trivially true for any value that equals its own header, which
        is far too weak a signal to delete a row over.
      * A single matching row is never removed -- it is at least as likely
        to be a real record as a repeated header.
      * A file whose every row matches the headers is left alone entirely;
        that pattern means it is a headerless file whose data happens to
        be words, not a file with a repeated header.
    """
    if len(df) < 2 or len(df.columns) == 0:
        # BUG FIX: this early return used to be silent, so the audit log
        # gave no explanation for why repeated-header removal did nothing.
        log_step(log, "remove_repeated_header_rows",
                 f"skipped -- only {len(df)} row(s) and {len(df.columns)} column(s) "
                 f"available, not enough context to safely detect a repeated header")
        return df

    check_cols = [c for c in df.columns if str(c).strip().lower() != "source_file"]
    if not check_cols:
        log_step(log, "remove_repeated_header_rows", "0 exact repeated header row(s) found")
        return df

    if len(check_cols) < 2:
        log_step(log, "remove_repeated_header_rows",
                 "skipped -- fewer than 2 comparable columns, so a value/hdr match "
                 "would be too weak a signal to safely delete a row")
        return df

    headers = [re.sub(r"\s+", "_", str(c).strip().lower()) for c in check_cols]

    sub = df[check_cols]
    normalized = sub.apply(
        lambda s: s.map(lambda v: "" if pd.isna(v) else re.sub(r"\s+", "_", str(v).strip().lower()))
    )

    mask = pd.Series(True, index=df.index)
    for col, hdr in zip(check_cols, headers):
        mask &= (normalized[col] == hdr)

    count = int(mask.sum())

    if count == 0:
        log_step(log, "remove_repeated_header_rows", "0 exact repeated header row(s) found")
        return df

    if count < 2:
        examples = df.loc[mask].head(3).astype(str).to_dict(orient="records")
        log_step(log, "remove_repeated_header_rows",
                 f"{count} row(s) matched the headers exactly but were KEPT -- a single match "
                 f"is more likely to be a legitimate record than a repeated header: {examples}")
        return df

    if count >= len(df):
        log_step(log, "remove_repeated_header_rows",
                 f"{count} row(s) matched the headers but removing them would empty the file -- "
                 f"KEPT (this looks like headerless data, not a repeated header)")
        return df

    examples = df.loc[mask].head(3).astype(str).to_dict(orient="records")
    df = df.loc[~mask].reset_index(drop=True)
    log_step(log, "remove_repeated_header_rows",
             f"{count} exact repeated header row(s) removed: {examples}")
    return df


def remove_total_rows(df, log):
    """
    Removes high-confidence total/subtotal rows.

    IMPORTANT: a row like "Total | 10" can be a legitimate record. The
    previous version removed anything whose first column read "total" and
    which had at least ONE populated data cell -- that is far too weak.
    A genuine total or subtotal almost always spans MULTIPLE numeric
    columns, so we now require at least two populated data cells before
    the row is even considered, and at least one of them must actually
    be numeric.
    """
    if df.empty or len(df.columns) < 2:
        log_step(log, "remove_total_rows", "0 high-confidence total/subtotal row(s) found")
        return df

    total_labels = {
        "total", "totals", "grand total", "grand totals",
        "subtotal", "sub-total", "sub total",
        "grand subtotal", "grand sub-total", "grand sub total"
    }

    data_cols = [c for c in df.columns if c != "source_file"]
    if not data_cols:
        log_step(log, "remove_total_rows", "0 high-confidence total/subtotal row(s) found")
        return df

    first_col = data_cols[0]
    first_norm = df[first_col].map(
        lambda v: "" if pd.isna(v) else re.sub(r"\s+", " ", str(v).strip().lower())
    )
    label_hit = first_norm.isin(total_labels)
    if not label_hit.any():
        log_step(log, "remove_total_rows", "0 high-confidence total/subtotal row(s) found")
        return df

    others = df.loc[label_hit, data_cols[1:]]
    if others.shape[1] == 0:
        log_step(log, "remove_total_rows", "0 high-confidence total/subtotal row(s) found")
        return df

    # BUG FIX (round 2): an earlier revision called `.str.strip()` on
    # `others` directly, but `others` is a DataFrame and DataFrames have no
    # `.str` accessor -- only Series do -- so that raised
    # "'DataFrame' object has no attribute 'str'" at runtime.
    # The ORIGINAL version used `.ne("nan")` which was also wrong (it
    # treated the literal string "nan" as blank). The fix is to strip
    # whitespace PER COLUMN via `.apply(...)` (which still returns a
    # DataFrame), then compare elementwise. `others.notna()` already
    # excludes real missing cells, so no separate "nan" handling is needed.
    others_str = others.astype(str).apply(lambda s: s.str.strip())
    populated_others = others.notna() & (others_str != "")
    # Require at least 2 populated data cells (beyond the label column).
    # A single-cell row like "Total | 10" is kept -- it can be a real record.
    populated_count = populated_others.sum(axis=1)
    enough_populated = populated_count >= 2
    candidate_index = enough_populated[enough_populated].index

    if len(candidate_index) == 0:
        log_step(log, "remove_total_rows",
                 "0 high-confidence total/subtotal row(s) found "
                 "(rows matching a total label were kept because they had fewer "
                 "than 2 populated data cells -- too weak a signal to delete)")
        return df

    def numeric_like_value(value):
        if pd.isna(value):
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        text = str(value).strip().replace(",", "")
        text = re.sub(r"[£$€]", "", text)
        text = text.replace("%", "")
        try:
            float(text)
            return True
        except (ValueError, TypeError):
            return False

    remove = []
    for idx in candidate_index:
        row = df.loc[idx]
        vals = [row[c] for c in data_cols[1:] if not pd.isna(row[c]) and str(row[c]).strip() != ""]
        if any(numeric_like_value(v) for v in vals):
            remove.append(idx)

    count = len(remove)
    if count:
        examples = df.loc[remove].head(5).astype(str).to_dict(orient="records")
        df = df.drop(index=remove).reset_index(drop=True)
        log_step(log, "remove_total_rows",
                 f"{count} high-confidence total/subtotal row(s) removed: {examples}")
    else:
        log_step(log, "remove_total_rows", "0 high-confidence total/subtotal row(s) found")
    return df


def standardize_casing(df, log):
    """
    Applies the configured per-column casing rule (lower / title / upper)
    when the column NAME contains a matching keyword.

    Two BUG FIXES from earlier versions:

    1. The keyword-to-column match is now word-boundary-based
       (_col_name_matches_keyword), not a plain substring test. Previously
       a column named "username" matched the "name" rule and had every
       value title-cased -- that silently rewrote case-sensitive
       identifiers such as usernames and login codes.

    2. Only string values are rewritten. Previously `before.str.lower()` /
       `.str.title()` / `.str.upper()` returned NaN for any non-string
       cell in an object column (e.g. a "customer_name" column containing
       ["John", 123, "Jane"]), which blanked the numeric entries and tripped
       the pipeline's data-loss guard with no obvious cause. Now non-string
       values are passed through unchanged.
    """
    for col in df.columns:
        col_lower = str(col).lower()
        for keyword, rule in CASING_RULES.items():
            if (
                _col_name_matches_keyword(col_lower, keyword)
                and str(df[col].dtype) in ("object", "str", "string")
            ):
                before = df[col]
                if rule == "lower":
                    after = before.map(lambda v: v.lower() if isinstance(v, str) else v)
                elif rule == "title":
                    after = before.map(lambda v: v.title() if isinstance(v, str) else v)
                elif rule == "upper":
                    after = before.map(lambda v: v.upper() if isinstance(v, str) else v)
                else:
                    after = before

                changed_mask = _safe_changed_mask(before, after)
                changed_count = int(changed_mask.sum())
                df[col] = after
                if changed_count:
                    examples = list(before[changed_mask].unique()[:3])
                    log_step(log, "standardize_casing",
                             f"{col}: {changed_count} value(s) converted to {rule} case (e.g. {examples})")
                break
    return df


def apply_category_mapping(df, mappings, log):
    """(unchanged from original)"""
    for col, mapping in mappings.items():
        if col in df.columns:
            lookup = {str(k).strip().lower(): v for k, v in mapping.items()}

            def resolve(v):
                if pd.isna(v):
                    return v
                return lookup.get(str(v).strip().lower(), v)

            before = df[col]
            after = before.map(resolve)
            changed_mask = _safe_changed_mask(before, after)
            changed_count = int(changed_mask.sum())
            df[col] = after
            if changed_count:
                changed_from = sorted(set(before[changed_mask].unique().tolist()))
                log_step(log, "category_value_mapping",
                         f"{col}: {changed_count} value(s) mapped {changed_from} -> canonical values")
    return df


def resolve_duplicate_columns(df, log, match_threshold=DUPLICATE_COLUMN_MATCH_THRESHOLD):
    """(unchanged from original)"""
    dupe_groups = {}
    for col in df.columns:
        match = re.match(r"^(.*)\.(\d+)$", str(col))
        if match:
            base = match.group(1)
            if base in df.columns:
                dupe_groups.setdefault(base, []).append(col)

    drops = []
    renames = {}
    total_rows = len(df)

    for base, dupes in dupe_groups.items():
        alt_counter = 0
        for dupe_col in dupes:
            same = (df[base] == df[dupe_col]) | (df[base].isna() & df[dupe_col].isna())
            match_rate = same.mean()
            if match_rate >= match_threshold:
                drops.append(dupe_col)
                log_step(log, "resolve_duplicate_columns",
                         f"{dupe_col} removed -- every one of {total_rows} row(s) "
                         f"is identical to {base}, so it carried no extra data")
            else:
                mismatches = int((~same).sum())
                alt_counter += 1
                new_name = f"{base}_alt_{alt_counter}"
                taken = set(df.columns) - {dupe_col}
                taken |= set(renames.values())
                while new_name in taken:
                    alt_counter += 1
                    new_name = f"{base}_alt_{alt_counter}"
                renames[dupe_col] = new_name
                log_step(log, "resolve_duplicate_columns",
                         f"{dupe_col} renamed to {new_name} -- differs from {base} "
                         f"in {mismatches} of {total_rows} row(s), so both columns are kept")

    if drops:
        df = df.drop(columns=drops)
    if renames:
        df = df.rename(columns=renames)
    return df


def detect_duplicate_content_columns(df, log):
    """(unchanged from original)"""
    duplicate_map = {}
    cols = [c for c in df.columns if c != "source_file"]
    pairs_found = []

    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            col_a, col_b = cols[i], cols[j]
            series_a, series_b = df[col_a], df[col_b]

            both_present = series_a.notna() & series_b.notna()
            if not both_present.any():
                continue

            a_pop = series_a[both_present]
            b_pop = series_b[both_present]
            try:
                same = a_pop == b_pop
                is_identical = bool(same.all())
            except (TypeError, ValueError):
                is_identical = a_pop.astype(str).equals(b_pop.astype(str))

            if is_identical:
                pairs_found.append((col_a, col_b))
                duplicate_map.setdefault(col_a, []).append(col_b)
                duplicate_map.setdefault(col_b, []).append(col_a)

    if pairs_found:
        for col_a, col_b in pairs_found:
            log_step(log, "detect_duplicate_content_columns",
                     f"'{col_a}' and '{col_b}' contain identical data in every row where "
                     f"both are populated, despite having different field names -- "
                     f"both headers flagged for review (BLUE)")
    else:
        log_step(log, "detect_duplicate_content_columns",
                 "0 column pair(s) with identical data under different field names found")

    return duplicate_map


def fix_encoding_issues(df, log):
    """(unchanged from original)"""
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
    for col in df.select_dtypes(include=["object"]).columns:
        before = df[col]
        is_str = before.map(lambda v: isinstance(v, str))
        non_ascii = before.map(lambda v: isinstance(v, str) and not v.isascii())
        if not non_ascii.any():
            continue
        cleaned = before.copy()
        cleaned.loc[non_ascii] = before[non_ascii].map(normalize_value)
        # BUG FIX: route through _safe_changed_mask so a nullable-boolean
        # or object-dtype comparison result can never poison the mask.
        # is_str keeps non-string cells out of the "changed" count (their
        # value was never touched in the first place).
        changed_mask = _safe_changed_mask(before, cleaned) & is_str
        changed_count = int(changed_mask.sum())
        if changed_count:
            affected_cols.append(col)
            total_changed += changed_count
        df[col] = cleaned
    detail = f"{total_changed} value(s) normalized"
    detail += f" across columns: {affected_cols}" if affected_cols else " (no special characters found)"
    log_step(log, "fix_encoding_issues", detail)
    return df


# ---------------------------------------------------------------------------
# format_phone_numbers -- (unchanged; the narrow candidate-column match and
# the phone-like-rate check already prevent the "PART-001 -> 001" style loss)
# ---------------------------------------------------------------------------
def format_phone_numbers(df, log):
    for col in df.columns:
        col_lower = str(col).lower()
        tokens = [t for t in re.split(r"[^a-z0-9]+", col_lower) if t]
        is_named_phone = any(t in PHONE_NAME_TOKENS for t in tokens)
        is_bare_number = col_lower in PHONE_EXACT_NAMES
        if not (is_named_phone or is_bare_number):
            continue
        if str(df[col].dtype) not in ("object", "str", "string"):
            continue

        before = df[col]
        non_null = before.dropna()
        if len(non_null) == 0:
            continue

        phone_like_rate = non_null.map(
            lambda v: bool(_PHONE_VALUE_RE.match(str(v).strip()))
        ).mean()

        if phone_like_rate < PHONE_MIN_LIKE_RATE:
            log_step(log, "format_phone_numbers",
                     f"{col}: skipped -- only {phone_like_rate:.0%} of values look phone-like, "
                     f"left untouched to avoid destroying non-phone data")
            continue

        def clean(v):
            if pd.isna(v):
                return v
            s = str(v).strip()
            if not _PHONE_VALUE_RE.match(s):
                # Never rewrite a value that isn't phone-shaped.
                return v
            digits = re.sub(r"\D", "", s)
            if digits.startswith("44"):
                digits = "0" + digits[2:]
            elif digits.startswith("7") and len(digits) == 10:
                digits = "0" + digits
            return digits

        after = before.map(clean)
        changed_mask = _safe_changed_mask(before, after)
        changed_count = int(changed_mask.sum())
        df[col] = after
        if changed_count:
            log_step(log, "format_phone_numbers",
                     f"{col}: {changed_count} value(s) standardized to digits-only")
        else:
            log_step(log, "format_phone_numbers",
                     f"{col}: 0 value(s) changed (already standardized)")
    return df