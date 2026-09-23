# steps/structure.py
"""
Steps that change the SHAPE of the table: repeated header rows, total rows,
duplicate rows, and blank rows. (Fully blank columns are intentionally kept,
not removed -- see drop_blank_rows_and_columns.) Every step is
step(df, ctx) -> df.
"""

import re
import pandas as pd

from config import APP_NUMBER_KEYWORDS, DESCRIPTION_KEYWORDS
from ..audit import log_step
from ..models import SOURCE_FILE_COL
from ..textutil import name_has_keyword, name_is_keyword, is_disguised_blank, casefold_for_compare


def _snake(value):
    return re.sub(r"\s+", "_", str(value).strip().lower())


def find_blank_columns(df, ctx):
    """
    Detect every fully blank column ONCE and store it on ctx.blank_cols.

    This must run after the column names are final, and before any per-cell
    step. Every later step reads ctx.blank_cols and skips those columns
    entirely, instead of re-testing 22k cells per column at every stage.

    On a wide sparse file (e.g. 22k rows x 400 cols where most fields are
    blank), this is the single biggest enabler of the speedup: without it,
    every per-cell step (casefold-for-dedupe, trim, dtype inference, date
    detection, price masking, ...) walks all 400 columns.
    """
    ctx.blank_cols = {c for c in df.columns if df[c].isna().all()}
    log_step(ctx.log, "find_blank_columns",
             f"{len(ctx.blank_cols)} fully blank column(s) detected -- skipped by "
             f"per-cell steps and by the issue sheets; still present in the "
             f"Cleaned and Raw output")
    return df


def remove_repeated_header_rows(df, ctx):
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
    log = ctx.log
    if len(df) < 2 or len(df.columns) == 0:
        log_step(log, "remove_repeated_header_rows",
                 f"skipped -- only {len(df)} row(s) and {len(df.columns)} column(s) "
                 f"available, not enough context to safely detect a repeated header")
        return df

    check_cols = [
        c for c in df.columns
        if str(c).strip().lower() != SOURCE_FILE_COL and c not in ctx.blank_cols
    ]
    if not check_cols:
        log_step(log, "remove_repeated_header_rows", "0 exact repeated header row(s) found")
        return df

    if len(check_cols) < 2:
        log_step(log, "remove_repeated_header_rows",
                 "skipped -- fewer than 2 comparable columns, so a value/hdr match "
                 "would be too weak a signal to safely delete a row")
        return df

    headers = [_snake(c) for c in check_cols]

    sub = df[check_cols]
    normalized = sub.apply(
        lambda s: s.map(lambda v: "" if pd.isna(v) else _snake(v))
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


_TOTAL_LABELS = {
    "total", "totals", "grand total", "grand totals",
    "subtotal", "sub-total", "sub total",
    "grand subtotal", "grand sub-total", "grand sub total",
}


def _numeric_like_value(value):
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


def remove_total_rows(df, ctx):
    """
    Removes high-confidence total/subtotal rows.

    IMPORTANT: a row like "Total | 10" can be a legitimate record. A genuine
    total or subtotal almost always spans MULTIPLE numeric columns, so a row
    is only considered when its first column reads as a total label AND at
    least two other data cells are populated AND at least one of them is
    numeric. Anything weaker is kept.
    """
    log = ctx.log
    none_found = "0 high-confidence total/subtotal row(s) found"

    if df.empty or len(df.columns) < 2:
        log_step(log, "remove_total_rows", none_found)
        return df

    data_cols = [c for c in df.columns if c != SOURCE_FILE_COL]
    if not data_cols:
        log_step(log, "remove_total_rows", none_found)
        return df

    first_col = data_cols[0]
    first_norm = df[first_col].map(
        lambda v: "" if pd.isna(v) else re.sub(r"\s+", " ", str(v).strip().lower())
    )
    label_hit = first_norm.isin(_TOTAL_LABELS)
    if not label_hit.any():
        log_step(log, "remove_total_rows", none_found)
        return df

    others = df.loc[label_hit, data_cols[1:]]
    if others.shape[1] == 0:
        log_step(log, "remove_total_rows", none_found)
        return df

    others_str = others.astype(str).apply(lambda s: s.str.strip())
    populated_others = others.notna() & (others_str != "")
    populated_count = populated_others.sum(axis=1)
    enough_populated = populated_count >= 2
    candidate_index = enough_populated[enough_populated].index

    if len(candidate_index) == 0:
        log_step(log, "remove_total_rows",
                 "0 high-confidence total/subtotal row(s) found "
                 "(rows matching a total label were kept because they had fewer "
                 "than 2 populated data cells -- too weak a signal to delete)")
        return df

    remove = []
    for idx in candidate_index:
        row = df.loc[idx]
        vals = [row[c] for c in data_cols[1:] if not pd.isna(row[c]) and str(row[c]).strip() != ""]
        if any(_numeric_like_value(v) for v in vals):
            remove.append(idx)

    count = len(remove)
    if count:
        examples = df.loc[remove].head(5).astype(str).to_dict(orient="records")
        df = df.drop(index=remove).reset_index(drop=True)
        log_step(log, "remove_total_rows",
                 f"{count} high-confidence total/subtotal row(s) removed: {examples}")
    else:
        log_step(log, "remove_total_rows", none_found)
    return df


def drop_duplicate_rows(df, ctx):
    """
    Drops rows that are fully identical to an earlier row once case is
    ignored -- "Acme" and "ACME" in every other-wise matching row count as
    the same row. The first occurrence is kept as-is (whatever casing it
    happened to have); only the later case-insensitive repeat(s) are removed.

    Fully blank columns are skipped: their cells are all NaN and can never
    distinguish one row from another, so mapping casefold over them is pure
    waste. On a wide sparse file this is one of the biggest single time
    sinks in the pipeline.
    """
    rows_before = len(df)
    live_cols = [c for c in df.columns if c not in ctx.blank_cols]
    if not live_cols:
        ctx.stats["duplicate_rows_removed"] = 0
        log_step(ctx.log, "drop_duplicate_rows",
                 "0 duplicate row(s) removed (every column is blank)")
        return df
    case_folded = df[live_cols].map(casefold_for_compare)
    keep_mask = ~case_folded.duplicated(keep="first")
    df = df[keep_mask].reset_index(drop=True)
    ctx.stats["duplicate_rows_removed"] = rows_before - len(df)
    log_step(ctx.log, "drop_duplicate_rows",
             f"{ctx.stats['duplicate_rows_removed']} duplicate row(s) removed "
             f"(case-insensitive comparison, {len(ctx.blank_cols)} blank column(s) skipped)")
    return df


def _text_len(value):
    """Length of a cell's trimmed text; 0 for a blank/NaN cell or a placeholder such as 'N/A'."""
    return 0 if pd.isna(value) or is_disguised_blank(value) else len(str(value).strip())


def dedupe_by_app_number(df, ctx):
    """
    App numbers have to be unique. If the file has a column that IS the app
    number (exactly: app / app_number / app_num / app_no -- see
    config.APP_NUMBER_KEYWORDS) AND at least one description-like column
    (description / part_description / item_description -- see
    config.DESCRIPTION_KEYWORDS), duplicate app numbers are collapsed down to
    a single row: whichever row has the LONGEST description, on the
    assumption that a longer description is the more complete record.

    IMPORTANT, all by design -- each one exists so this step can never delete
    a row on a coin flip:
      * BOTH an app-number column and a description column must exist.
      * The app-number column is matched by EXACT name.
      * A duplicate group is only collapsed if at least one of its rows has
        a non-blank description.
      * If there are several description-like columns their text lengths are
        added together.
      * Blank/NaN app numbers -- and placeholders such as N/A or TBD -- are
        never grouped with each other.
    """
    log = ctx.log
    ctx.stats.setdefault("duplicate_app_numbers_collapsed", 0)
    data_cols = [c for c in df.columns if c != SOURCE_FILE_COL]

    app_cols = [c for c in data_cols if name_is_keyword(c, APP_NUMBER_KEYWORDS)]
    if not app_cols:
        log_step(log, "dedupe_by_app_number",
                 "skipped -- no app-number column (app / app_number / "
                 "app_num / app_no) found")
        return df
    app_col = app_cols[0]
    extra_app_note = (f" (other app-number-like column(s) {app_cols[1:]} were not "
                      f"used as the key)") if len(app_cols) > 1 else ""

    desc_cols = [c for c in data_cols
                 if c != app_col and name_has_keyword(c, DESCRIPTION_KEYWORDS)]
    if not desc_cols:
        log_step(log, "dedupe_by_app_number",
                 f"'{app_col}' is an app-number column but no description-like "
                 f"column (description / part_description / item_description) was found "
                 f"-- duplicate app numbers were KEPT, since there's no safe way to "
                 f"choose between them{extra_app_note}")
        return df

    app_key = df[app_col].map(lambda v: "" if pd.isna(v) or is_disguised_blank(v) else str(v).strip())
    desc_len = sum(df[c].map(_text_len) for c in desc_cols)

    keyed_mask = app_key != ""
    dup_mask = keyed_mask & app_key.duplicated(keep=False)

    none_found = (f"0 duplicate app number(s) found in '{app_col}' -- nothing to group "
                  f"(blank/NaN app numbers, if any, are never grouped){extra_app_note}")
    if not dup_mask.any():
        log_step(log, "dedupe_by_app_number", none_found)
        return df

    group_max_len = desc_len.groupby(app_key.to_numpy()).transform("max")
    collapse_mask = dup_mask & (group_max_len > 0)
    undecidable_mask = dup_mask & ~collapse_mask
    undecidable_keys = sorted(set(app_key[undecidable_mask]))
    undecidable_note = ""
    if undecidable_keys:
        undecidable_note = (
            f" {len(undecidable_keys)} duplicated app number(s) had NO description "
            f"in any of their rows, so all their rows were KEPT (flagged BLUE for review): "
            f"{undecidable_keys[:5]}{' ...' if len(undecidable_keys) > 5 else ''}")

    if not collapse_mask.any():
        log_step(log, "dedupe_by_app_number",
                 f"0 app number row(s) removed from '{app_col}'.{undecidable_note}{extra_app_note}")
        return df

    picker = pd.DataFrame({
        "app_key": app_key[collapse_mask].to_numpy(),
        "desc_len": desc_len[collapse_mask].to_numpy(),
        "order": range(int(collapse_mask.sum())),
    }, index=df.index[collapse_mask])

    keep_idx = (
        picker.sort_values(["desc_len", "order"], ascending=[False, True])
              .groupby("app_key")
              .head(1)
              .index
    )
    drop_idx = picker.index.difference(keep_idx)
    kept_idx = picker.index.intersection(keep_idx)

    example_cols = [app_col] + desc_cols
    dropped_examples = df.loc[drop_idx, example_cols].head(5).astype(str).to_dict(orient="records")
    kept_examples = df.loc[kept_idx, example_cols].head(5).astype(str).to_dict(orient="records")
    dupe_group_count = picker["app_key"].nunique()
    df = df.drop(index=drop_idx).reset_index(drop=True)
    ctx.stats["duplicate_app_numbers_collapsed"] = len(drop_idx)
    used = desc_cols[0] if len(desc_cols) == 1 else desc_cols
    log_step(log, "dedupe_by_app_number",
             f"{len(drop_idx)} duplicate app number row(s) removed across "
             f"{dupe_group_count} app number(s) in '{app_col}' (longest {used!r} wins per "
             f"app number). Kept: {kept_examples}. Dropped: {dropped_examples}."
             f"{undecidable_note}{extra_app_note}")
    return df


def drop_blank_rows_and_columns(df, ctx):
    """
    Removes rows where EVERY data cell is blank (source_file doesn't count).
    Fully-blank COLUMNS are intentionally left in place -- they used to be
    dropped here, but a blank column may just not have been filled in yet,
    so it's kept as-is and is NOT flagged blue (flagging every blank column
    blue would be noisy on files that have many of them).
    """
    rows_before = len(df)
    data_cols = [c for c in df.columns if c != SOURCE_FILE_COL]
    if data_cols:
        blank_row_mask = df[data_cols].isna().all(axis=1)
        df = df.loc[~blank_row_mask].reset_index(drop=True)
    ctx.stats["blank_rows_removed"] = rows_before - len(df)
    ctx.stats["blank_cols_removed"] = 0
    log_step(ctx.log, "drop_blank_rows_and_columns",
             f"{ctx.stats['blank_rows_removed']} blank row(s) removed "
             f"(blank columns are kept, not removed)")
    return df