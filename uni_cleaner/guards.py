# guards.py
"""
Data-loss safety gates.

DATA LOSS SHOULD ONLY COME FROM (see TODO.txt):
  1. fully duplicate rows/cols
  2. fully blank rows (fully blank COLUMNS are kept, never dropped)
  3. placeholder-to-blank conversions (N/A, TBD, -, ...) -- SWITCHED OFF for now:
     placeholders are kept and flagged PURPLE instead (see steps/values.py)
  4. filler rows above the header
Anything else that makes a populated value vanish is a bug, and the guards
turn it into a loud failure instead of silent corruption.
"""

import functools


def assert_no_unexpected_value_loss(before, after, step_name, disguised_blanks=None):
    """
    Safety gate for value-only cleaning steps.

    For every surviving cell, a source value that was genuinely populated may
    not become missing. The only permitted exception is a value explicitly
    classified as a disguised blank. This catches accidental data destruction
    from .str operations, coercion, date parsing, boolean mapping, encoding
    repair, etc.

    The check is deliberately performed only when row/column identity is
    unchanged. Structural removals are validated separately by the pipeline
    and are limited to the documented loss categories.
    """
    if before.shape != after.shape or list(before.columns) != list(after.columns):
        raise ValueError(
            f"DATA LOSS GUARD [{step_name}]: shape/columns changed during a "
            "value-only operation."
        )

    disguised = set(str(v).strip().lower() for v in (disguised_blanks or []))

    for col in before.columns:
        b = before[col]
        a = after[col]

        # Cheap, fully vectorized "did this column change at all?" check.
        # Most value-only steps here only ever touch a handful of columns
        # (a phone column, the columns a casing rule matched, ...), but this
        # guard used to run the expensive part below -- including a
        # per-cell Python .map() hunting for disguised-blank text -- over
        # EVERY column of the dataframe, on EVERY value-only step. On a wide
        # file that's the hundreds of untouched columns re-scanned for
        # nothing, once per step. Series.equals() is a single vectorized
        # comparison (NaN == NaN counts as equal there), so an untouched
        # column is ruled out in one cheap pass instead of a slow per-cell
        # scan; only columns that actually changed pay for the full check.
        try:
            unchanged = b.equals(a)
        except Exception:
            unchanged = False
        if unchanged:
            continue

        before_present = b.notna()
        if disguised:
            before_present &= ~b.map(
                lambda v: isinstance(v, str) and v.strip().lower() in disguised
            )

        lost = before_present & a.isna()
        if lost.any():
            examples = b[lost].head(10).tolist()
            raise ValueError(
                f"DATA LOSS GUARD [{step_name}]: {int(lost.sum())} populated "
                f"value(s) became blank in column '{col}'. Examples: {examples}"
            )


def assert_structural_loss_allowed(before, after, step_name, allowed_row_drop=0, allowed_col_drop=0):
    """
    Safety gate for structural operations: verifies that row/column counts
    changed by no more than the explicitly permitted amount.

    NOTE: nothing calls this yet (it wasn't wired in before the refactor
    either). It is the natural home for a check on drop_duplicate_rows /
    drop_blank_rows_and_columns -- see the notes in the refactor summary.
    """
    row_drop = before.shape[0] - after.shape[0]
    col_drop = before.shape[1] - after.shape[1]
    if row_drop < 0 or col_drop < 0:
        raise ValueError(
            f"DATA LOSS GUARD [{step_name}]: rows/columns were unexpectedly added."
        )
    if row_drop > allowed_row_drop or col_drop > allowed_col_drop:
        raise ValueError(
            f"DATA LOSS GUARD [{step_name}]: unexpected structural loss. "
            f"Rows dropped={row_drop}, allowed={allowed_row_drop}; "
            f"columns dropped={col_drop}, allowed={allowed_col_drop}."
        )


def value_only(allow_blanks=None):
    """
    Decorator for pipeline steps that may rewrite values but must never
    destroy them. It snapshots the dataframe, runs the step, then runs
    assert_no_unexpected_value_loss on before/after. This replaces the

        before = df.copy()
        df = some_step(df, log)
        assert_no_unexpected_value_loss(before, df, "some_step")

    block that used to be pasted by hand around every value-only step in
    pipeline.py -- decorate the step once and it is guarded everywhere.

    `allow_blanks`: values that are ALLOWED to become blank (only the
    disguised-blank conversion needs this).
    """
    def decorator(step):
        @functools.wraps(step)
        def guarded(df, ctx):
            before = df.copy()
            after = step(df, ctx)
            assert_no_unexpected_value_loss(
                before, after, step.__name__, disguised_blanks=allow_blanks
            )
            return after
        return guarded
    return decorator
