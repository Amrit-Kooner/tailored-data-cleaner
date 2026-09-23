# models.py
"""
Small shared data structures. No logic lives here.
"""

from dataclasses import dataclass, field

import pandas as pd

# The column the pipeline adds to record which file each row came from.
SOURCE_FILE_COL = "source_file"

# Columns the pipeline itself owns: type inference and price-flagging skip them.
RESERVED_COLUMNS = (SOURCE_FILE_COL, "missing_count")


@dataclass
class RunContext:
    """
    Everything a step needs besides the dataframe itself. Every pipeline step
    has the signature  step(df, ctx) -> df  and reads/writes shared state
    through this object instead of through ever-longer argument lists.
    """
    source_name: str
    log: dict
    stats: dict = field(default_factory=dict)
    fail_mask: "pd.DataFrame | None" = None
    date_cols: list = field(default_factory=list)
    duplicate_name_map: dict = field(default_factory=dict)
    unnamed_col_positions: list = field(default_factory=list)
    money_cols: list = field(default_factory=list)
    number_display: dict = field(default_factory=dict)
    # Column names whose HEADER is drawn BLUE for review. Steps append to this:
    # several description columns, and any column whose data is identical to
    # another column's under a different name. Order matters (it drives the
    # Field Anomalies sheet), so this stays a list -- but every step that adds
    # to it needs a "have I already flagged this column?" check first, and
    # `col in blue_header_cols` on a list is O(n) per check (O(n^2) total
    # across n columns). blue_header_cols_seen mirrors the same contents as a
    # set purely for that O(1) membership test; see add_blue_header_col().
    blue_header_cols: list = field(default_factory=list)
    blue_header_cols_seen: set = field(default_factory=set)
    # {column: [note line, ...]} -- the text of the Excel note on that BLUE header.
    blue_header_notes: dict = field(default_factory=dict)
    # Columns that are entirely blank (every cell NaN). Computed ONCE by
    # steps.structure.find_blank_columns and used by every later per-cell step
    # to skip work on columns that carry no data. They are still kept in the
    # final output (Cleaned / Raw) -- this set only avoids touching them.
    blank_cols: set = field(default_factory=set)


def add_blue_header_col(ctx, col_name):
    """
    Append `col_name` to ctx.blue_header_cols the first time it is seen,
    using ctx.blue_header_cols_seen (a set) for an O(1) "already flagged"
    check instead of `col_name in ctx.blue_header_cols` (O(n) on a list,
    O(n^2) total over many calls). List order (first-flagged-first) is
    preserved exactly as before.
    """
    if col_name not in ctx.blue_header_cols_seen:
        ctx.blue_header_cols_seen.add(col_name)
        ctx.blue_header_cols.append(col_name)


@dataclass
class CleanResult:
    """What clean_file() hands back (this replaces the old 9-item tuple)."""
    df: pd.DataFrame
    original_df: pd.DataFrame
    log: dict
    summary: str
    date_cols: list
    missing_mask: pd.DataFrame
    fail_mask: pd.DataFrame
    review_mask: pd.DataFrame
    price_review_mask: pd.DataFrame
    app_duplicate_mask: pd.DataFrame
    app_invalid_mask: pd.DataFrame
    duplicate_name_map: dict
    money_cols: list = field(default_factory=list)
    number_display: dict = field(default_factory=dict)
    blue_header_cols: list = field(default_factory=list)
    blue_header_notes: dict = field(default_factory=dict)
    disguised_mask: "pd.DataFrame | None" = None