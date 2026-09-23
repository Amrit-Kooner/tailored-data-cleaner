# steps/columns.py
"""
Steps that deal with the COLUMNS themselves: flagging odd headers,
standardised names, and duplicate columns. Every step is  step(df, ctx) -> df.
"""

import re
import pandas as pd

from config import DUPLICATE_COLUMN_MATCH_THRESHOLD
from ..audit import log_step
from ..models import SOURCE_FILE_COL, add_blue_header_col
from ..textutil import normalize_name, casefold_for_compare, is_unnamed_column


def flag_problem_columns(df, ctx):
    """
    Flags (never removes) pandas-suffixed duplicate headers and unnamed
    columns. Also records WHERE the unnamed ones are (by position) so
    flag_unnamed_columns can flag them BLUE by their final name, after
    standardize_column_names / dedupe_column_names have renamed them.
    """
    renamed = [col for col in df.columns if re.search(r"\.\d+$", str(col))]
    log_step(ctx.log, "check_duplicate_fields",
             f"{len(renamed)} duplicate column name(s) flagged: {renamed}")

    unnamed_cols = [col for col in df.columns if is_unnamed_column(col)]
    ctx.unnamed_col_positions = [i for i, col in enumerate(df.columns) if is_unnamed_column(col)]
    log_step(ctx.log, "check_unnamed_columns",
             f"{len(unnamed_cols)} unnamed column(s) flagged: {unnamed_cols}")
    return df


def standardize_column_names(df, ctx):
    """
    lowercase, underscore-separated, whitespace stripped. Headers are coerced
    to str first, so a numeric header cell (e.g. the year 2024) is handled.
    """
    old_cols = list(df.columns)
    df.columns = [normalize_name(c) for c in old_cols]
    renamed_pairs = [f"'{o}' -> '{n}'" for o, n in zip(old_cols, df.columns) if str(o) != n]
    log_step(ctx.log, "standardize_column_names",
             f"{len(renamed_pairs)} column name(s) standardized"
             + (f": {renamed_pairs}" if renamed_pairs else ""))
    return df


def dedupe_names_with_suffixes(names):
    """
    Makes every name in `names` unique by appending ".1", ".2", ".3", ...
    to any repeat. The first occurrence keeps its original name; the second
    becomes "name.1", the third "name.2", and so on. Every name that already
    exists anywhere in the source list is *reserved* up front, so a
    generated suffix can never collide with a column the file itself
    provided (e.g. a real "email.1" column).
    """
    reserved = set(names)
    seen = set()
    counts = {}
    result = []
    for name in names:
        if name not in seen:
            seen.add(name)
            counts[name] = 0
            result.append(name)
            continue
        counts[name] += 1
        new_name = f"{name}.{counts[name]}"
        while new_name in reserved or new_name in seen:
            counts[name] += 1
            new_name = f"{name}.{counts[name]}"
        seen.add(new_name)
        counts[new_name] = 0
        result.append(new_name)
    return result


def dedupe_column_names(df, ctx):
    """Names that collide AFTER standardisation get .N suffixes."""
    deduped_names = dedupe_names_with_suffixes(list(df.columns))
    dupe_renames = [f"'{o}' -> '{n}'" for o, n in zip(df.columns, deduped_names) if o != n]
    df.columns = deduped_names
    log_step(ctx.log, "dedupe_standardized_column_names",
             f"{len(dupe_renames)} column name(s) that collided after standardization "
             f"given .N suffixes" + (f": {dupe_renames}" if dupe_renames else ""))
    return df


def flag_unnamed_columns(df, ctx):
    """
    Flags BLUE (for review, never removed) every column whose header was
    blank/unnamed in the source file, using the positions flag_problem_columns
    captured before renaming. Runs after standardize_column_names and
    dedupe_column_names so it always flags the column's FINAL name, even one
    that picked up a '.N' suffix from colliding with another blank header.
    """
    log = ctx.log
    cols = list(df.columns)
    flagged = []
    for pos in ctx.unnamed_col_positions:
        if pos >= len(cols):
            continue
        col_name = cols[pos]
        flagged.append(col_name)
        add_blue_header_col(ctx, col_name)
        ctx.blue_header_notes.setdefault(col_name, []).append(
            "Column header was blank/unnamed in the source file -- flagged for review."
        )
    if flagged:
        log_step(log, "flag_unnamed_columns",
                 f"{len(flagged)} unnamed column header(s) flagged for review (BLUE): {flagged}")
    else:
        log_step(log, "flag_unnamed_columns",
                 "0 unnamed column header(s) found -- nothing to flag")
    return df


def resolve_duplicate_columns(df, ctx, match_threshold=DUPLICATE_COLUMN_MATCH_THRESHOLD):
    """
    Only renames, never flags and never removes. For "email" / "email.1":
    if the two columns hold DIFFERENT data in at least one row, the suffixed
    copy is renamed "<base>_alt_N" so the two aren't confused for one another
    by name alone.

    Flagging of identical columns -- including identical same-name copies --
    is done in ONE place, by detect_duplicate_content_columns, using the
    single rule "exact same data in every row". This step no longer touches
    ctx.duplicate_name_map.
    """
    log = ctx.log
    dupe_groups = {}
    for col in df.columns:
        match = re.match(r"^(.*)\.(\d+)$", str(col))
        if match:
            base = match.group(1)
            if base in df.columns:
                dupe_groups.setdefault(base, []).append(col)

    renames = {}
    total_rows = len(df)

    for base, dupes in dupe_groups.items():
        alt_counter = 0
        for dupe_col in dupes:
            same = _cell_matches(df[base], df[dupe_col])
            if bool(same.all()):
                # Identical copy: keep the '.N' name; the general duplicate-
                # content step will flag both headers BLUE.
                continue
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

    if renames:
        df = df.rename(columns=renames)
    if not dupe_groups:
        log_step(log, "resolve_duplicate_columns",
                 "0 '.N'-suffixed duplicate column(s) found -- nothing to resolve")
    return df


def _cell_matches(series_a, series_b):
    """
    Row-by-row "same value" test between two columns, IGNORING CASE ("Acme",
    "acme" and "ACME" are the same value). 0, False and a blank are THREE
    different values: 0 never matches False, and neither matches a blank.
    A blank only matches a blank -- a blank against a value is a difference.
    """
    a = series_a.map(casefold_for_compare)
    b = series_b.map(casefold_for_compare)
    try:
        return a == b
    except (TypeError, ValueError):
        return a.astype(str).eq(b.astype(str))


def _is_all_blank(series):
    """True when every cell is NaN, empty, or whitespace-only."""
    for v in series:
        if pd.isna(v):
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        return False
    return True


def detect_duplicate_content_columns(df, ctx):
    """
    Flags every pair of columns whose data is the EXACT same in every row.
    Only the two HEADERS are flagged BLUE -- nothing is removed, no cell is
    coloured, no column is renamed here.

    The rule is the SAME for every pair, whatever the column names:
      * every row is compared, including rows where only one of the two
        columns has a value -- a value against a blank is a difference;
      * text is compared case-insensitively ("Acme"/"acme"/"ACME" are the
        same value), but 0, False and blank are three separate values and
        never match each other.

    A column whose every value is blank (NaN / empty / whitespace-only) is
    NEVER flagged, even when compared against another all-blank column.

    Performance: this used to compare every PAIR of columns directly
    (`for i ... for j ...`, re-casefolding column i from scratch on every
    inner-loop iteration even though that only depends on i, not j). On a
    wide file that is O(cols^2 x rows) Python-level casefold calls -- for
    400 columns that's ~80,000 pairs, each re-walking a 22k-row column
    twice. Instead, each column's casefolded values are computed ONCE
    (O(cols x rows) total) and used as a hashable signature (a tuple of
    casefold_for_compare() strings). Columns are then grouped by that exact
    signature with a plain dict lookup -- two columns are duplicates iff
    they land in the same group, so no direct pairwise comparison across
    the whole column set is needed at all. The tuple is compared by value
    equality (never a hash collision producing a false match), so results
    are identical to the old pairwise version, just without the O(cols^2)
    blowup.
    """
    log = ctx.log
    duplicate_map = dict(ctx.duplicate_name_map)
    already_flagged_pairs = {
        frozenset((col, other)) for col, others in duplicate_map.items() for other in others
    }
    # Mirrors duplicate_map's lists as sets, purely so "already recorded?"
    # below is an O(1) set lookup instead of `other in duplicate_map.get(col, [])`
    # (O(k) on a list, worse on a wide file where one column matches many
    # others). duplicate_map itself stays a {col: [list]} for the rest of
    # the pipeline/output, which doesn't care about order here anyway.
    duplicate_map_seen = {col: set(others) for col, others in duplicate_map.items()}
    cols = [c for c in df.columns if c != SOURCE_FILE_COL]

    blank_cols = {c for c in cols if _is_all_blank(df[c])}
    candidate_cols = [c for c in cols if c not in blank_cols]

    groups = {}
    for col in candidate_cols:
        signature = tuple(df[col].map(casefold_for_compare))
        groups.setdefault(signature, []).append(col)

    pairs_found = []
    for group_cols in groups.values():
        if len(group_cols) < 2:
            continue
        for i in range(len(group_cols)):
            for j in range(i + 1, len(group_cols)):
                col_a, col_b = group_cols[i], group_cols[j]

                pair_key = frozenset((col_a, col_b))
                if pair_key not in already_flagged_pairs:
                    pairs_found.append((col_a, col_b))
                if col_b not in duplicate_map_seen.get(col_a, ()):
                    duplicate_map.setdefault(col_a, []).append(col_b)
                    duplicate_map_seen.setdefault(col_a, set()).add(col_b)
                if col_a not in duplicate_map_seen.get(col_b, ()):
                    duplicate_map.setdefault(col_b, []).append(col_a)
                    duplicate_map_seen.setdefault(col_b, set()).add(col_a)

    present = set(df.columns)
    duplicate_map = {
        col: [other for other in others if other in present]
        for col, others in duplicate_map.items() if col in present
    }
    duplicate_map = {col: others for col, others in duplicate_map.items() if others}

    if pairs_found:
        for col_a, col_b in pairs_found:
            log_step(log, "detect_duplicate_content_columns",
                     f"'{col_a}' and '{col_b}' have the exact same data in every row "
                     f"(ignoring case) -- both headers flagged for review (BLUE)")
    else:
        log_step(log, "detect_duplicate_content_columns",
                 "0 column pair(s) with identical data in every row found")

    ctx.duplicate_name_map = duplicate_map
    return df