# steps/descriptions.py
"""
flag_multiple_description_columns   when a file has SEVERAL description
                                    columns, flags all their HEADERS BLUE
                                    for review.

Every step is  step(df, ctx) -> df.

"Description fields" = every column whose name matches config.DESCRIPTION_KEYWORDS
(part_description, item_description, description, ...).
"""

from config import DESCRIPTION_KEYWORDS
from ..audit import log_step
from ..textutil import name_has_keyword
from ..models import add_blue_header_col


def find_description_columns(df):
    """Every column, left to right, whose name matches DESCRIPTION_KEYWORDS."""
    return [col for col in df.columns if name_has_keyword(col, DESCRIPTION_KEYWORDS)]


def flag_multiple_description_columns(df, ctx):
    """
    When a file has MORE THAN ONE description-like column, every one of their
    HEADERS is flagged BLUE for review, with an Excel note listing the other
    description fields it is the same kind of field as. Only headers are
    coloured.
    """
    log = ctx.log
    cols = find_description_columns(df)
    ctx.stats["multi_description_columns_flagged"] = 0
    if len(cols) < 2:
        log_step(log, "flag_multiple_description_columns",
                 "no description column found -- nothing to flag" if not cols else
                 f"only one description column ('{cols[0]}') -- nothing to flag")
        return df

    for col in cols:
        add_blue_header_col(ctx, col)
        others = [c for c in cols if c != col]
        ctx.blue_header_notes.setdefault(col, []).append(
            f"Same kind of field as: {', '.join(others)}. The file has several description fields."
        )
    ctx.stats["multi_description_columns_flagged"] = len(cols)
    log_step(log, "flag_multiple_description_columns",
             f"{len(cols)} description fields found ({', '.join(cols)}) -- headers flagged for review (BLUE)")
    return df
