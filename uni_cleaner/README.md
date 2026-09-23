# uni_cleaner

The cleaning engine behind `main.py`. Settings stay in `config.py` (project root).

## Where things went (old flat file -> new home)

| Old                                   | New                                                     |
|---------------------------------------|---------------------------------------------------------|
| `main.py` (batch loop)                | `batch.py`                                              |
| `main.py` (log-file writing)          | `output/report.py::write_audit_log`                     |
| `pipeline.py::clean_file` (300 lines) | `pipeline.py` (ordered step lists) + `steps/*`          |
| `pipeline.py::_open_excel/_detect_separator` | `loaders.py`                                     |
| `cleaning.py` header/row steps        | `loaders.py::find_header_row`, `steps/structure.py`     |
| `cleaning.py` column steps            | `steps/columns.py`                                      |
| `cleaning.py` value steps             | `steps/values.py`                                       |
| `dtype_detection.py` `looks_like_*`   | `detectors.py`                                          |
| `dtype_detection.py` dates            | `steps/dates.py`                                        |
| `dtype_detection.py` conversion       | `steps/dtypes.py`                                       |
| `excel_output.py`                     | `output/excel.py`                                       |
| `utils.py` report builders            | `output/report.py`                                      |
| `utils.py` guards                     | `guards.py`                                              |
| `utils.py` encoding / IO helpers      | `loaders.py`, `files.py`                                |
| `utils.py` `log_step`                 | `audit.py`                                              |

## Adding a cleaning step

1. Write `def my_step(df, ctx): ... return df` in the matching `steps/` module.
   Use `ctx.log` / `log_step(...)` for the audit trail and `ctx.stats` for numbers
   the report should show.
2. If it rewrites values but must never destroy them, decorate it `@value_only()`.
3. Add it to `STRUCTURE_STEPS` (or `TYPE_STEPS`) in `pipeline.py` at the position
   you want it to run.

## Import rule

Modules only import "downward":
`models/audit/textutil` -> `guards/detectors/masks` -> `loaders`/`steps/*`/`output/*` -> `pipeline` -> `batch` -> `main.py`.
`config.py` is imported as a top-level module, so run the project through `main.py`.

## Multiple description columns

`steps/descriptions.py` is a pure rule-based flagging step -- no AI, no new
column is generated. When a file has MORE THAN ONE description-like column
(any column matching `DESCRIPTION_KEYWORDS`, e.g. `description`,
`part_description`, `item_description`), every one of their HEADERS is
flagged **BLUE** for review. **Every BLUE header also carries an Excel note**
(hover over it) saying which other description field(s) it is the same kind
of field as. Only BLUE headers get a note -- no data cell ever does. A column
whose data is identical to another column's under a different name is
flagged BLUE the same way, by `steps/columns.py`.

This project has no AI or local-model dependency: nothing generates a
description, a unit, a product family or a sub-inventory value. Every
cleaning step is deterministic and rule-based.
