# output/excel.py
"""
Writes one cleaned workbook with the core sheets first:

    Cleaned
    Raw
    Report
    Generated Fields

Then separate issue sheets:

    Field Duplicate Values
    ID Duplicates
    ID Anomalies
    Price Anomalies
    Failed Values
    Missing Values
    Disguised Blanks
    Field Anomalies

Every issue category gets its own sheet and its own colour, and YELLOW is
reserved for price anomalies only:
  * field duplicate values (whole columns identical)   -> green header
  * duplicate APP values                                -> pink (PINK is reserved for APP)
  * blank / invalid / unexpected APP values             -> lavender
  * price anomalies such as zero/negative values        -> yellow

Row 1 of every issue sheet is a single merged explainer cell describing
what the colour on that sheet means; the real header sits on row 2 and the
data starts on row 3. Cleaned / Raw / Report are data-only sheets, so they
keep their header on row 1.

The Raw tab is deliberately BLACK on purpose, and left uncoloured inside.
Placeholder/blank APP values are excluded from the Disguised Blanks sheet --
they only ever appear on the APP-specific sheets above.

The Missing Values sheet was previously commented out on wide sparse files
because writing a full-width copy of the dataset just to paint some cells
orange dominated write time -- it is now written back in, but note that
_write_mask_sheet only writes columns that carry information in the
affected subset (see below), so this stays cheap even on wide sheets.

The issue sheets only write columns that carry information in that
subset -- a column is written if it is flagged somewhere or populated
somewhere in the affected rows. On a wide sparse sheet this is the
difference between 8.8M blank cells and the few hundred that matter.

Row traceability: the Cleaned sheet has a leading "row_number" column, a plain
1..N count of its data rows. Every other sheet that lists actual data rows
(the issue/mask sheets, Field Duplicate Values, Generated Fields) carries a
matching leading "cleaned_row_number" column, so a flagged row on any sheet can
be traced straight back to its exact row on Cleaned -- important once a
sheet is only showing a filtered subset of rows, where that sheet's own
row position no longer lines up with the row's real position in the file.
Field Anomalies is field-level, not row-level (it flags whole columns), so
it has no row-reference column.
"""

from datetime import datetime
import numbers
import re
import pandas as pd
import openpyxl
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

APP_ID_NAMES = {"app", "app_number", "app_num", "app_no"}

from ..models import SOURCE_FILE_COL
from .report import build_report_lines, SECTION_TITLES

FAILED_FILL = PatternFill(start_color="FFB3B3", end_color="FFB3B3", fill_type="solid")
MISSING_FILL = PatternFill(start_color="FFD9A6", end_color="FFD9A6", fill_type="solid")
DISGUISED_FILL = PatternFill(start_color="D5B3FF", end_color="D5B3FF", fill_type="solid")

DUPLICATE_VALUE_FILL = PatternFill(start_color="92D050", end_color="92D050", fill_type="solid")
APP_DUPLICATE_FILL = PatternFill(start_color="F4B6C2", end_color="F4B6C2", fill_type="solid")
APP_INVALID_FILL = PatternFill(start_color="CDB4DB", end_color="CDB4DB", fill_type="solid")
PRICE_FILL = PatternFill(start_color="FFE599", end_color="FFE599", fill_type="solid")
HEADER_REVIEW_FILL = PatternFill(start_color="D9EAF7", end_color="D9EAF7", fill_type="solid")
GENERATED_FILL = PatternFill(start_color="C6E0B4", end_color="C6E0B4", fill_type="solid")

NUMBER_FORMAT_2DP = "0.00"
REPORT_FONT = Font(name="Consolas", size=10)
EXPLAINER_FONT = Font(bold=True, italic=True)

# Report sheet styling only (see _write_report_sheet): the text itself is
# unchanged plain lines from build_report_lines, these just colour-band it
# for readability. REPORT_SECTION_FILL intentionally matches
# TAB_COLOR_REPORT below, and the legend fills below reuse the exact same
# PatternFill objects the issue sheets use, so the Report sheet's colours
# always match what the rest of the workbook actually shows.
REPORT_BANNER_FILL = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
REPORT_BANNER_FONT = Font(name="Consolas", size=10, bold=True, color="FFFFFF")
REPORT_BANNER_TITLE_FONT = Font(name="Consolas", size=13, bold=True, color="FFFFFF")
REPORT_SECTION_FILL = PatternFill(start_color="9DC3E6", end_color="9DC3E6", fill_type="solid")
REPORT_SECTION_FONT = Font(name="Consolas", size=10, bold=True, color="1F3864")
REPORT_SUBRULE_FONT = Font(name="Consolas", size=10, color="BFBFBF")
REPORT_PASS_FILL = PatternFill(start_color="C6E0B4", end_color="C6E0B4", fill_type="solid")
REPORT_PASS_FONT = Font(name="Consolas", size=10, bold=True, color="375623")
_AUDIT_LOG_TITLE_RE = re.compile(r"^AUDIT LOG \(\d+ steps?\)$")

# The Cleaned sheet gets a leading 1..N row number so a row can be pointed
# to unambiguously; every issue/review sheet that lists actual data rows
# (not just field names) carries a matching reference column back to it, so
# a flagged cell on any sheet can be traced to its exact row on Cleaned.
ROW_NUMBER_COL = "row_number"
CLEANED_ROW_REF_COL = "cleaned_row_number"

TAB_COLOR_CLEANED = "FFFFFF"
TAB_COLOR_RAW = "000000"
TAB_COLOR_REPORT = "9DC3E6"
TAB_COLOR_DUPLICATE = "92D050"
TAB_COLOR_APP_DUPLICATE = "F4B6C2"
TAB_COLOR_APP_INVALID = "CDB4DB"
TAB_COLOR_PRICE = "FFE599"
TAB_COLOR_FAILED = "FFB3B3"
TAB_COLOR_MISSING = "FFD9A6"
TAB_COLOR_DISGUISED = "D5B3FF"
TAB_COLOR_HEADER_REVIEW = "D9EAF7"
TAB_COLOR_GENERATED = "C6E0B4"

# Row-1 explainer text for every coloured sheet. Kept here so all wording
# lives in one place, next to the fill it describes.
EXPLAINER_GENERATED = (
    "Green column headers = field created by the cleaner (it was not present in "
    "the raw file)."
)
EXPLAINER_DUPLICATE_VALUES = (
    "Green column headers = fields whose data is identical to another field's, "
    "in every row."
)
EXPLAINER_APP_DUPLICATE = (
    "Pink cells = duplicate APP: the same APP number appears more than once "
    "(APP is expected to be unique)."
)
EXPLAINER_APP_INVALID = (
    "Lavender cells = APP ID anomaly: the APP value is blank or a placeholder "
    "such as N/A / TBD / unknown."
)
EXPLAINER_PRICE = (
    "Yellow cells = price anomaly: a zero or negative value, or a non-numeric "
    "value, in a numeric price field."
)
EXPLAINER_FAILED = (
    "Red cells = value failed to parse/convert and was left exactly as it "
    "appeared in the source file (never removed)."
)
EXPLAINER_MISSING = (
    "Orange cells = missing value (blank in the cleaned output)."
)
EXPLAINER_DISGUISED = (
    "Purple cells = disguised blank: placeholder text such as N/A, TBD, "
    "unknown, '-' -- kept as-is, never removed or turned into a blank."
)
EXPLAINER_HEADER = (
    "Light blue rows = field name flagged for review: unnamed in the source, "
    "an identical duplicate of another field, or one of several description fields."
)

# How far right row 1's explainer banner is merged, in columns -- far past
# any sheet's actual data, so the banner always reads as one full-width row
# instead of stopping wherever that sheet's real columns happen to end.
EXPLAINER_MERGE_WIDTH = 1000


def _write_cell(ws, row, column, value):
    cell = ws.cell(row=row, column=column, value=value)
    if cell.data_type == "f":
        cell.data_type = "s"
    return cell


def _write_explainer(ws, text):
    """
    Row 1 of every coloured sheet: what the colour on this sheet means.
    Always merged out to EXPLAINER_MERGE_WIDTH columns -- far past any
    sheet's actual data -- so the banner reads as one full-width row
    instead of stopping wherever that sheet's real columns happen to end.
    """
    cell = _write_cell(ws, 1, 1, text)
    cell.font = EXPLAINER_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=EXPLAINER_MERGE_WIDTH)
    return cell


NOTE_AUTHOR = "Cleaner"


def _add_note(cell, lines):
    """
    Attaches an Excel cell comment -- the little red-triangle note you get
    on hover -- to a highlighted HEADER cell, explaining why that field was
    flagged. `lines` is a list of separate reasons (a field can be flagged
    for more than one reason); each becomes its own line in the note.

    Sized roughly to the text instead of Excel's tiny default box, so the
    reason is readable without the user having to drag the corner first.
    """
    lines = [str(line) for line in lines if line]
    if not lines:
        return
    text = "\n".join(lines)
    comment = Comment(text, NOTE_AUTHOR)
    longest_line = max(len(line) for line in lines)
    comment.width = max(180, min(400, longest_line * 6 + 20))
    comment.height = max(60, 18 * (len(lines) + 1))
    cell.comment = comment


DATE_STRING_FORMATS = ("%d/%m/%Y", "%d/%m/%y")


def _is_real_number(value):
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, numbers.Number):
        try:
            return not pd.isna(value)
        except (TypeError, ValueError):
            return True
    return False


def _parse_date_string(value):
    for fmt in DATE_STRING_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _write_value_cell(ws, excel_row, col_idx, col_name, value, date_cols, money_cols, number_display):
    if col_name in date_cols and isinstance(value, str) and value.strip():
        real_date = _parse_date_string(value)
        if real_date is not None:
            cell = ws.cell(row=excel_row, column=col_idx, value=real_date)
            cell.number_format = "DD/MM/YYYY"
            return cell

    excel_value = None if pd.isna(value) else value
    cell = _write_cell(ws, excel_row, col_idx, excel_value)

    if _is_real_number(value):
        if col_name in set(money_cols or ()):
            cell.number_format = NUMBER_FORMAT_2DP
        else:
            decimals = (number_display or {}).get(col_name, {}).get(float(value))
            if decimals:
                cell.number_format = "0." + "0" * decimals
    return cell


def _autosize_columns(ws, df, start_col=1):
    for i, col_name in enumerate(df.columns):
        col_idx = start_col + i
        max_len = max(
            len(str(col_name)),
            df[col_name].astype(str).str.len().max() if len(df) > 0 else 0
        )
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 40)


def _write_plain_sheet(ws, df, date_cols=(), money_cols=(), number_display=None,
                       row_number_col=None):
    """
    Header on row 1, data from row 2 -- no explainer (used for Cleaned / Raw).

    `row_number_col`: when given, an extra leading column is written first
    holding a plain 1..N count of the data rows (Cleaned uses this -- see
    ROW_NUMBER_COL). Row N here is always Excel row N+2 on this sheet, so
    any other sheet can point back to an exact row with a single number,
    without depending on row position (rows on the issue sheets are a
    filtered subset, so their own row position doesn't match Cleaned's).
    """
    col_offset = 1
    if row_number_col:
        header = _write_cell(ws, 1, 1, row_number_col)
        header.font = Font(bold=True)
        col_offset = 2

    for col_idx, col_name in enumerate(df.columns, start=col_offset):
        cell = _write_cell(ws, 1, col_idx, col_name)
        cell.font = Font(bold=True)

    for row_pos, row in enumerate(df.itertuples(index=False)):
        excel_row = row_pos + 2
        if row_number_col:
            _write_cell(ws, excel_row, 1, row_pos + 1)
        for col_idx, (col_name, value) in enumerate(zip(df.columns, row), start=col_offset):
            _write_value_cell(ws, excel_row, col_idx, col_name, value,
                              date_cols, money_cols, number_display)

    _autosize_columns(ws, df, start_col=col_offset)
    if row_number_col:
        ws.column_dimensions[get_column_letter(1)].width = max(10, len(row_number_col) + 2)


def _write_mask_sheet(
    ws, df, mask, fill, explainer,
    date_cols=(), money_cols=(), number_display=None,
    header_fill_cols=None, context_app_col=None, context_label=None
):
    """
    Write rows containing a specific issue and colour only that issue.

    Layout:
        row 1  -- merged explainer
        row 2  -- column headers
        row 3+ -- data rows with the issue cells coloured

    Only columns that carry information in THIS subset are written: a column
    that is flagged somewhere in the subset, or populated somewhere in the
    subset. On a wide sparse sheet this is the difference between writing
    8.8M blank cells and writing the few hundred that matter.

    Every row also carries a leading CLEANED_ROW_REF_COL value: the row's
    1-based position on the Cleaned sheet (df's index is a clean 0..N-1
    range by the time it reaches here, reset after every row-dropping step
    -- see steps/structure.py -- so index + 1 always matches Cleaned's own
    ROW_NUMBER_COL for that row). This sheet only ever shows a filtered
    subset of rows, so the row's position here tells you nothing about
    where it sits in the full file; this column is what lets a flagged
    cell be traced back to its exact row on Cleaned.
    """
    header_fill_cols = set(header_fill_cols or ())
    date_cols = set(date_cols)
    money_cols = set(money_cols or ())
    has_context = bool(context_app_col) and context_app_col in df.columns

    if mask is None:
        row_has_flag = pd.Series(False, index=df.index)
    else:
        row_has_flag = mask.any(axis=1)

    subset_df = df.loc[row_has_flag]
    subset_mask = mask.loc[row_has_flag] if mask is not None else None

    if subset_df.empty:
        write_cols = []
    else:
        flagged_any = subset_mask.any(axis=0) if subset_mask is not None else pd.Series(False, index=df.columns)
        populated_any = subset_df.notna().any(axis=0)
        write_cols = [
            c for c in df.columns
            if bool(flagged_any.get(c, False)) or bool(populated_any.get(c, False))
        ]

    _write_explainer(ws, explainer)

    if not write_cols:
        _write_cell(ws, 2, 1, "No issues found for this category.")
        ws.column_dimensions["A"].width = 40
        return

    header_row = 2
    row_ref_header = _write_cell(ws, header_row, 1, CLEANED_ROW_REF_COL)
    row_ref_header.font = Font(bold=True)
    col_offset = 2
    for col_idx, col_name in enumerate(write_cols, start=col_offset):
        cell = _write_cell(ws, header_row, col_idx, col_name)
        cell.font = Font(bold=True)
        if col_name in header_fill_cols:
            cell.fill = fill

    col_positions = {c: i for i, c in enumerate(df.columns)}
    write_positions = [(c, col_positions[c]) for c in write_cols]

    for row_pos, (row_idx, row) in enumerate(
        zip(subset_df.index, subset_df.itertuples(index=False)), start=3
    ):
        _write_cell(ws, row_pos, 1, int(row_idx) + 1)
        for col_idx, (col_name, pos) in enumerate(write_positions, start=col_offset):
            cell = _write_value_cell(
                ws, row_pos, col_idx, col_name, row[pos],
                date_cols, money_cols, number_display
            )
            if bool(subset_mask.at[row_idx, col_name]):
                cell.fill = fill

    _autosize_columns(ws, subset_df[write_cols], start_col=col_offset)
    ws.column_dimensions[get_column_letter(1)].width = max(10, len(CLEANED_ROW_REF_COL) + 2)

    if has_context:
        context_header = context_label or "APP ID"
        context_col_idx = len(write_cols) + col_offset
        header = _write_cell(ws, header_row, context_col_idx, context_header)
        header.font = Font(bold=True)
        for row_pos, row_idx in enumerate(subset_df.index, start=3):
            _write_cell(ws, row_pos, context_col_idx, df.at[row_idx, context_app_col])
        values = [df.at[idx, context_app_col] for idx in subset_df.index]
        max_len = max([len(context_header)] + [len(str(v)) for v in values]) if values else len(context_header)
        ws.column_dimensions[get_column_letter(context_col_idx)].width = min(max_len + 2, 40)


def _write_duplicate_value_sheet(ws, df, duplicate_name_map,
                                 date_cols=(), money_cols=(), number_display=None):
    """
    Field-level issue: two or more columns hold identical data in every row.
    Only the affected field names (header row) are shaded green -- the data
    cells underneath are left uncoloured.

    Only the duplicate columns are written at all. The old version wrote the
    entire dataframe (8.8M cells on a wide sheet) just to shade some header
    cells green; this writes the handful of columns that actually matter.

    Layout: row 1 merged explainer, row 2 headers, row 3+ data. Every row
    carries a leading CLEANED_ROW_REF_COL back to its row on Cleaned (see
    _write_mask_sheet for why).
    """
    duplicate_cols = set(duplicate_name_map or {})
    write_cols = [c for c in df.columns if c in duplicate_cols]
    if not write_cols:
        _write_explainer(ws, EXPLAINER_DUPLICATE_VALUES)
        _write_cell(ws, 2, 1, "No duplicate value fields found.")
        ws.column_dimensions["A"].width = 40
        return

    date_cols = set(date_cols)
    money_cols = set(money_cols or ())

    _write_explainer(ws, EXPLAINER_DUPLICATE_VALUES)

    header_row = 2
    row_ref_header = _write_cell(ws, header_row, 1, CLEANED_ROW_REF_COL)
    row_ref_header.font = Font(bold=True)
    col_offset = 2
    for col_idx, col_name in enumerate(write_cols, start=col_offset):
        cell = _write_cell(ws, header_row, col_idx, col_name)
        cell.font = Font(bold=True)
        cell.fill = DUPLICATE_VALUE_FILL
        others = (duplicate_name_map or {}).get(col_name, [])
        _add_note(cell, [
            f"Duplicate of: {', '.join(others)}" if others else "",
            "Same data in every row (case-insensitive).",
        ])

    subset = df[write_cols]
    for row_pos, (row_idx, row) in enumerate(
        zip(subset.index, subset.itertuples(index=False)), start=3
    ):
        _write_cell(ws, row_pos, 1, int(row_idx) + 1)
        for col_idx, (col_name, value) in enumerate(zip(write_cols, row), start=col_offset):
            _write_value_cell(
                ws, row_pos, col_idx, col_name, value,
                date_cols, money_cols, number_display
            )

    _autosize_columns(ws, subset, start_col=col_offset)
    ws.column_dimensions[get_column_letter(1)].width = max(10, len(CLEANED_ROW_REF_COL) + 2)


def _write_header_review_sheet(ws, df, header_cols, notes):
    """
    Field-name-only review items: unnamed headers, duplicate-content fields,
    multi-description fields. Just the field name is listed, so the sheet
    stays scannable -- the "why" text is attached as an Excel comment on
    each cell (hover or click to read it) rather than printed inline.

    Layout: row 1 explainer, row 2+ one field name per row.
    """
    _write_explainer(ws, EXPLAINER_HEADER)

    if not header_cols:
        _write_cell(ws, 2, 1, "No field anomalies items found.")
        ws.column_dimensions["A"].width = 40
        return

    row = 2
    for col in header_cols:
        cell = _write_cell(ws, row, 1, col)
        cell.fill = HEADER_REVIEW_FILL
        _add_note(cell, notes.get(col, []))
        row += 1
    ws.column_dimensions["A"].width = 60


# Legend swatches for the "THINGS TO REVIEW" colour key: maps each colour
# NAME as report.py writes it to the exact same PatternFill used on the
# real issue sheet, so the report's key is guaranteed to match reality.
_LEGEND_FILLS = {
    "ORANGE": MISSING_FILL,
    "RED": FAILED_FILL,
    "PURPLE": DISGUISED_FILL,
    "PINK": APP_DUPLICATE_FILL,
    "GREEN": DUPLICATE_VALUE_FILL,
    "LAVENDER": APP_INVALID_FILL,
    "YELLOW": PRICE_FILL,
}
_LEGEND_LINE_RE = re.compile(r"^\s*(ORANGE|RED|PURPLE|PINK|GREEN|LAVENDER|YELLOW)\s*=")


def _write_report_sheet(ws, source_filename, dataset_summary, log):
    """
    Writes the audit report one line per row -- the text itself is exactly
    build_report_lines' output, unchanged -- then layers colour on top
    purely so it's easier to scan:

      * the top/bottom "====" banners get a dark navy fill and bold white
        text;
      * each section title (SOURCE FILE, DATASET OVERVIEW, ...) and its
        underline get a light-blue band matching the Report tab's own
        colour, so the sections are easy to jump between at a glance;
      * the "COLOUR = meaning" legend lines inside THINGS TO REVIEW are
        swatched with the exact same PatternFill used on the sheet they
        describe, so the key can never drift out of sync with reality;
      * the data-loss-guard PASSED line gets a green flag;
      * the lone "-" rule lines used as mid-section separators are dimmed
        to a light grey instead of solid black, so they read as a rule
        rather than another line of content.

    A plain-text copy/paste of this sheet still reads exactly as before --
    nothing here changes what build_report_lines returns.
    """
    lines = build_report_lines(source_filename, dataset_summary, log)
    in_banner = False
    expect_underline = False
    for row_idx, line in enumerate(lines, start=1):
        cell = _write_cell(ws, row_idx, 1, line)
        cell.font = REPORT_FONT
        stripped = line.strip()

        if stripped and set(stripped) == {"="}:
            in_banner = not in_banner
            cell.fill = REPORT_BANNER_FILL
            cell.font = REPORT_BANNER_FONT
            expect_underline = False
            continue

        if in_banner:
            cell.fill = REPORT_BANNER_FILL
            cell.font = (
                REPORT_BANNER_TITLE_FONT
                if stripped in ("DATA CLEANING REPORT", "End of report")
                else REPORT_BANNER_FONT
            )
            continue

        if stripped in SECTION_TITLES or _AUDIT_LOG_TITLE_RE.match(stripped):
            cell.fill = REPORT_SECTION_FILL
            cell.font = REPORT_SECTION_FONT
            expect_underline = True
            continue

        if expect_underline and len(stripped) >= 4 and set(stripped) == {"-"}:
            cell.fill = REPORT_SECTION_FILL
            expect_underline = False
            continue
        expect_underline = False

        if stripped == "-":
            cell.font = REPORT_SUBRULE_FONT
            continue

        legend_match = _LEGEND_LINE_RE.match(line)
        if legend_match:
            cell.fill = _LEGEND_FILLS[legend_match.group(1)]
            continue

        if "UNEXPECTED DATA LOSS GUARD" in line and "PASSED" in line:
            cell.fill = REPORT_PASS_FILL
            cell.font = REPORT_PASS_FONT

    ws.column_dimensions["A"].width = 120


def _find_app_id_column(df):
    """Return the exact APP identifier column used by the cleaner, if present."""
    for col in df.columns:
        if str(col).strip().lower() in APP_ID_NAMES:
            return col
    return None


def _write_generated_fields_sheet(ws, df, generated_cols, date_cols=(), money_cols=(), number_display=None):
    """
    Show the cleaner-generated column(s) with their data, laid out like a
    coloured issue sheet: row 1 merged explainer, row 2 headers (each
    generated field name shaded GREEN), row 3+ the values.

    This used to be a two-column "Field / Description" metadata table -- it
    is now just the generated column with its data, and its colour is
    explained on row 1 like every other coloured sheet. Every row carries a
    leading CLEANED_ROW_REF_COL back to its row on Cleaned (see
    _write_mask_sheet for why).
    """
    present = [c for c in generated_cols if c in df.columns]
    if not present:
        _write_explainer(ws, EXPLAINER_GENERATED)
        _write_cell(ws, 2, 1, "No generated fields.")
        ws.column_dimensions["A"].width = 40
        return

    _write_explainer(ws, EXPLAINER_GENERATED)

    header_row = 2
    row_ref_header = _write_cell(ws, header_row, 1, CLEANED_ROW_REF_COL)
    row_ref_header.font = Font(bold=True)
    col_offset = 2
    for col_idx, col_name in enumerate(present, start=col_offset):
        cell = _write_cell(ws, header_row, col_idx, col_name)
        cell.font = Font(bold=True)
        cell.fill = GENERATED_FILL
        _add_note(cell, [
            "Generated by the cleaner -- not present in the source file.",
            "Records which source file each row came from.",
        ])

    subset = df[present]
    for row_pos, (row_idx, row) in enumerate(
        zip(subset.index, subset.itertuples(index=False)), start=3
    ):
        _write_cell(ws, row_pos, 1, int(row_idx) + 1)
        for col_idx, (col_name, value) in enumerate(zip(present, row), start=col_offset):
            _write_value_cell(ws, row_pos, col_idx, col_name, value,
                              date_cols, money_cols, number_display)

    _autosize_columns(ws, subset, start_col=col_offset)
    ws.column_dimensions[get_column_letter(1)].width = max(10, len(CLEANED_ROW_REF_COL) + 2)


def save_as_formatted_excel(result, path, source_filename):
    """
    Sheet order is always:
      Cleaned -> Raw -> Report -> Generated Fields -> all other issue sheets,
      including Missing Values (ORANGE).
    """
    df = result.df

    wb = openpyxl.Workbook()

    ws_cleaned = wb.active
    ws_cleaned.title = "Cleaned"
    ws_cleaned.sheet_properties.tabColor = TAB_COLOR_CLEANED
    _write_plain_sheet(ws_cleaned, df, result.date_cols, result.money_cols, result.number_display,
                       row_number_col=ROW_NUMBER_COL)

    # Raw: black tab, uncoloured cells.
    ws_raw = wb.create_sheet("Raw")
    ws_raw.sheet_properties.tabColor = TAB_COLOR_RAW
    _write_plain_sheet(ws_raw, result.original_df)

    ws_report = wb.create_sheet("Report")
    ws_report.sheet_properties.tabColor = TAB_COLOR_REPORT
    _write_report_sheet(ws_report, source_filename, result.summary, result.log)

    ws_generated = wb.create_sheet("Generated Fields")
    ws_generated.sheet_properties.tabColor = TAB_COLOR_GENERATED
    _write_generated_fields_sheet(
        ws_generated,
        df,
        [SOURCE_FILE_COL],
        result.date_cols, result.money_cols, result.number_display,
    )

    ws_dupes = wb.create_sheet("Field Duplicate Values")
    ws_dupes.sheet_properties.tabColor = TAB_COLOR_DUPLICATE
    _write_duplicate_value_sheet(
        ws_dupes, df, result.duplicate_name_map,
        result.date_cols, result.money_cols, result.number_display
    )

    ws_app_dupe = wb.create_sheet("ID Duplicates")
    ws_app_dupe.sheet_properties.tabColor = TAB_COLOR_APP_DUPLICATE
    _write_mask_sheet(
        ws_app_dupe, df, result.app_duplicate_mask, APP_DUPLICATE_FILL,
        EXPLAINER_APP_DUPLICATE,
        result.date_cols, result.money_cols, result.number_display
    )

    ws_app_invalid = wb.create_sheet("ID Anomalies")
    ws_app_invalid.sheet_properties.tabColor = TAB_COLOR_APP_INVALID
    _write_mask_sheet(
        ws_app_invalid, df, result.app_invalid_mask, APP_INVALID_FILL,
        EXPLAINER_APP_INVALID,
        result.date_cols, result.money_cols, result.number_display
    )

    ws_price = wb.create_sheet("Price Anomalies")
    ws_price.sheet_properties.tabColor = TAB_COLOR_PRICE
    _write_mask_sheet(
        ws_price, df, result.price_review_mask, PRICE_FILL,
        EXPLAINER_PRICE,
        result.date_cols, result.money_cols, result.number_display
    )

    ws_failed = wb.create_sheet("Failed Values")
    ws_failed.sheet_properties.tabColor = TAB_COLOR_FAILED
    _write_mask_sheet(
        ws_failed, df, result.fail_mask, FAILED_FILL,
        EXPLAINER_FAILED,
        result.date_cols, result.money_cols, result.number_display
    )

    app_id_col = _find_app_id_column(df)

    ws_missing = wb.create_sheet("Missing Values")
    ws_missing.sheet_properties.tabColor = TAB_COLOR_MISSING
    _write_mask_sheet(
        ws_missing, df, result.missing_mask, MISSING_FILL,
        EXPLAINER_MISSING,
        result.date_cols, result.money_cols, result.number_display,
        context_app_col=app_id_col, context_label="ID (context)"
    )

    ws_disguised = wb.create_sheet("Disguised Blanks")
    ws_disguised.sheet_properties.tabColor = TAB_COLOR_DISGUISED
    _write_mask_sheet(
        ws_disguised, df, result.disguised_mask, DISGUISED_FILL,
        EXPLAINER_DISGUISED,
        result.date_cols, result.money_cols, result.number_display,
        context_app_col=app_id_col, context_label="ID (context)"
    )

    # Field-name-only review items (unnamed / duplicate / multi-description fields).
    header_cols = list(result.blue_header_cols or [])
    notes = dict(result.blue_header_notes or {})
    ws_header = wb.create_sheet("Field Anomalies")
    ws_header.sheet_properties.tabColor = TAB_COLOR_HEADER_REVIEW
    _write_header_review_sheet(ws_header, df, header_cols, notes)

    wb.save(path)