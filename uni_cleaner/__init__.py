"""
uni_cleaner -- the cleaning engine behind main.py.

Layout (each layer only imports from the layers above it):

    models.py       shared dataclasses + the reserved column names
    audit.py        the audit log (log_step)
    textutil.py     column-name / text helpers used everywhere
    guards.py       data-loss guards + the @value_only decorator
    detectors.py    "what kind of column is this?" predicates (no side effects)
    loaders.py      read a file from disk -> raw dataframe + header row
    masks.py        the RED / ORANGE / BLUE / PURPLE cell-flag masks
    steps/          one module per KIND of cleaning step; every step is
                    step(df, ctx) -> df
    pipeline.py     the ordered step lists + clean_file()
    output/         Excel writer, audit-report text, output-folder naming
    batch.py        run_batch(): the drop-folder loop that main.py calls
"""
