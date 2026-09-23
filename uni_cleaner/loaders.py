# loaders.py
"""
Reading a file from disk: which encoding / separator / sheet, and where the
real header row is. Returns a LoadedFile; nothing here cleans anything.
"""

import hashlib
import numbers
import os
import re
import time
from dataclasses import dataclass, field

import pandas as pd

from config import HEADER_DETECTION_THRESHOLD, EXCEL_ENGINES
from .textutil import normalize_name, disguised_blank_values

EXCEL_EXTENSIONS = set(EXCEL_ENGINES.keys())

FILE_TYPE_LABELS = {
    "csv":  "CSV (comma-separated text)",
    "tsv":  "TSV (tab-separated text)",
    "txt":  "Plain text",
    "xlsx": "Excel workbook (.xlsx)",
    "xls":  "Excel workbook (.xls, legacy binary)",
    "xlsm": "Excel macro-enabled workbook (.xlsm)",
    "xlsb": "Excel binary workbook (.xlsb)",
}


@dataclass
class LoadedFile:
    path: str
    ext: str
    original_df: pd.DataFrame     # the table as read, header row applied, otherwise untouched
    header_row: int               # number of filler rows skipped above the header
    encoding: "str | None" = None       # text files only
    sheet_name: "str | int | None" = None   # Excel only
    sheet_names: "list | None" = None       # Excel only
    sheet_note: "str | None" = None         # Excel only -- set when auto-sheet-selection kicked in
    # {column: {value: fixed decimals shown in the source}} -- see "Raw number display" below
    number_display: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Text files: encoding + separator
# ---------------------------------------------------------------------------
def detect_text_encoding(path, sample_bytes=200000):
    """
    Detects the text encoding of a file by inspecting its raw bytes, so the
    reader can be told the correct encoding explicitly instead of relying on
    the platform default (which differs between Windows and macOS/Linux and
    is a very common cause of mojibake in the first place).

    Tries, in order:
      1. An explicit BOM (UTF-8 BOM -> 'utf-8-sig', UTF-16 BOM -> 'utf-16')
      2. Strict UTF-8 (the modern default)
      3. Strict CP1252 (Windows-1252, the usual "Excel on Windows" export)
      4. Latin-1 as a last-resort fallback -- it can decode any byte sequence
         and never raises, so it is only reached when nothing else worked.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(sample_bytes)
    except OSError:
        return "utf-8"

    if not raw:
        return "utf-8"

    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"

    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    try:
        raw.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        pass

    return "latin-1"


def detect_separator(path, encoding):
    """
    Best-effort field-separator detection for a text file, so a .tsv is read
    as tab-separated and a European-style ';' or pipe-delimited export is not
    silently collapsed into one column.

    Up to 20 non-empty lines are inspected and the separator with the highest
    total count across those lines wins. Falls back to ',' if none is found.
    """
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            sample = f.read(65536)
    except OSError:
        return ","

    sep_counts = {sep: 0 for sep in ("\t", ";", "|", ",")}
    lines_checked = 0
    for line in sample.splitlines():
        if not line.strip():
            continue
        for sep in sep_counts:
            sep_counts[sep] += line.count(sep)
        lines_checked += 1
        if lines_checked >= 20:
            break

    if not lines_checked:
        return ","
    best = max(sep_counts, key=sep_counts.get)
    if sep_counts[best] > 0:
        return best
    return ","


# ---------------------------------------------------------------------------
# Excel files
# ---------------------------------------------------------------------------
def open_excel(path):
    ext = os.path.splitext(path)[1].lower()
    engine = EXCEL_ENGINES.get(ext)
    try:
        xl = pd.ExcelFile(path, engine=engine)
    except ImportError as e:
        package = engine or "the required engine package"
        raise RuntimeError(
            f"Cannot read {ext} file '{os.path.basename(path)}' -- the "
            f"'{package}' package is required. Install it with "
            f"'pip install {package}'."
        ) from e
    except Exception as e:
        raise RuntimeError(
            f"Could not open Excel file '{os.path.basename(path)}': {e}"
        ) from e
    return xl, list(xl.sheet_names)


# ---------------------------------------------------------------------------
# Sheet selection
# ---------------------------------------------------------------------------
def _sheet_usability_score(raw_df):
    """
    How much this sheet looks like an actual DATA table, as opposed to a
    cover/title/notes sheet with a stray cell or two: (row count after the
    detected header, header-like cell count in that row). A real table has
    a multi-column header and at least one row below it; a cover sheet
    ("Company Confidential" in A1) does not.
    """
    if len(raw_df) == 0:
        return (0, 0)
    header_row = find_header_row(raw_df)
    header_cells = raw_df.loc[header_row]
    header_like = sum(_looks_like_header_cell(v) for v in header_cells if not pd.isna(v))
    data_rows = len(raw_df) - header_row - 1
    return (max(data_rows, 0), header_like)


def _choose_sheet(xl, sheet_names):
    """
    BUG FIX: the loader used to always read sheet_names[0], full stop. A very
    common real-world layout is a cover/title sheet ("Cover", "Notes",
    "ReadMe") in front of the actual data sheet -- that sheet has one or two
    title cells and is otherwise blank. Reading it as-is produced a
    completely empty result with no error and no explanation, so the file
    silently "cleaned" to nothing while the real data on the next sheet was
    never even looked at.

    Only the genuinely broken case is changed: if the FIRST sheet has no
    usable table on it (see _sheet_usability_score -- no data rows, or fewer
    than 2 header-like cells) and at least one other sheet does, the most
    usable sheet is used instead. Any workbook whose first sheet actually
    has a real table keeps today's behaviour untouched.

    Returns (raw_df_of_chosen_sheet, chosen_sheet_name, note). `note` is
    None unless a sheet other than the first was picked.
    """
    first_name = sheet_names[0]
    first_raw = xl.parse(sheet_name=first_name, header=None)
    first_rows, first_hdr_cells = _sheet_usability_score(first_raw)
    if len(sheet_names) == 1 or (first_rows >= 1 and first_hdr_cells >= 2):
        return first_raw, first_name, None

    best_name, best_raw, best_score = first_name, first_raw, (first_rows, first_hdr_cells)
    for name in sheet_names[1:]:
        raw = xl.parse(sheet_name=name, header=None)
        score = _sheet_usability_score(raw)
        if score > best_score:
            best_name, best_raw, best_score = name, raw, score

    if best_name == first_name:
        # Nothing else looked more usable; fall back to the original choice.
        return first_raw, first_name, None

    note = (f"sheet '{first_name}' had no usable table (looked like a "
            f"cover/title sheet) -- used sheet '{best_name}' instead, "
            f"the most populated of {sheet_names}")
    return best_raw, best_name, note


# ---------------------------------------------------------------------------
# Header-row detection
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Raw number display ("keep .00 if the source had it")
# ---------------------------------------------------------------------------
# pandas throws away how a number was DISPLAYED in the source: a cell holding
# 4.3 formatted "0.00" (shows 4.30), or the text "4.30", both arrive as 4.3.
# So that non-price numbers can be written back looking exactly like the raw
# file, the loader also records, per column, which values were shown with
# padded decimals:   { normalized_column_name: { 4.3: 2, 100.0: 2, ... } }
# Only values whose display had MORE decimals than the number naturally has
# (4.30, 100.00) are recorded; a value shown in Excel's General format (4.3,
# 100, 5.25) has no entry and is written as General.
_NUMERIC_TEXT_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_FORMAT_NOISE_RE = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.|_.|\*.')
_FORMAT_NOT_PLAIN_NUMBER_RE = re.compile(r"[%EeDdMmYyHhSs@/]")
_FIXED_DECIMALS_RE = re.compile(r"\.(0+)(?![0#?])")


def fixed_decimals_from_format(number_format):
    """
    Decimal places an Excel number format shows, or None when the format is
    General / a date / a percentage / scientific / text / has optional
    decimals ("0.0#") / shows no decimals -- i.e. whenever there is no
    fixed-decimals display worth preserving.

        "0.00" -> 2     "#,##0.00" -> 2     "GBP"#,##0.000 -> 3
        "General" -> None   "0" -> None   "0.0#" -> None   "dd/mm/yyyy" -> None
    """
    if not number_format or number_format == "General":
        return None
    section = _FORMAT_NOISE_RE.sub("", str(number_format).split(";")[0])
    if _FORMAT_NOT_PLAIN_NUMBER_RE.search(section):
        return None
    match = _FIXED_DECIMALS_RE.search(section)
    return len(match.group(1)) if match else None


def _natural_decimals(number):
    """Decimals the number has with no padding: 4.3 -> 1, 100.0 -> 0, 5.25 -> 2. None if not simple."""
    text = repr(float(number))
    if "e" in text or "E" in text:
        return None
    return len(text.split(".")[1].rstrip("0")) if "." in text else 0


def _note_display(store, col_key, number, decimals):
    """
    Remember that `number` was displayed with `decimals` fixed decimals -- but
    only when that is MORE than the number naturally has ("4.30", "100.00").
    A value like 5.25 shown as "5.25" looks identical in General, so it needs
    no explicit format.
    """
    natural = _natural_decimals(number)
    if not decimals or natural is None or decimals <= natural:
        return
    col = store.setdefault(col_key, {})
    if decimals > col.get(number, 0):
        col[number] = decimals


def _note_numeric_text(store, col_key, text):
    """A text cell such as '4.30' or '1,400.50': remember how many decimals it was typed with."""
    s = text.strip()
    if _NUMERIC_TEXT_RE.match(s):
        frac = s.split(".", 1)[1] if "." in s else ""
        _note_display(store, col_key, float(s.replace(",", "")), len(frac))


def _excel_extras_pass(path, sheet_name, header_row, df):
    """
    Second, read-only pass over an .xlsx/.xlsm doing BOTH jobs that used to be
    two entirely separate full scans of the same sheet:

      1. number-display info: the display decimals of numeric cells (from
         their number format) and of numeric-looking text cells -- this used
         to be _excel_number_display().
      2. boolean restoration: pandas turns a column mixing real TRUE/FALSE
         cells with blanks or numbers into float64 (TRUE -> 1.0, FALSE -> 0.0),
         so a FALSE becomes indistinguishable from a genuine 0 by the time the
         cleaner sees it -- this used to be _restore_excel_booleans().

    Both need the same thing (every cell's raw openpyxl value and format,
    walked in the same row/column order), so doing them as two separate
    `load_workbook(..., read_only=True)` + `ws.iter_rows()` passes meant
    opening the file twice and visiting every cell twice -- on a wide sheet
    (e.g. 22k rows x 400 cols = 8.8M cells) that's 17.6M cell visits for
    information that a single pass collects just as well. This is that
    single pass; `df` is returned with any booleans restored, alongside the
    number_display dict.

    Never raises: on any problem this returns (df unchanged, {}) and the
    caller falls back to Excel's General format / pandas' own float read.
    """
    keys = [normalize_name(c) for c in df.columns]
    number_display = {}
    bool_found = {}
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[sheet_name] if isinstance(sheet_name, str) else wb.worksheets[sheet_name]
            for row_idx, row in enumerate(ws.iter_rows()):
                if row_idx <= header_row:
                    continue
                pos = row_idx - header_row - 1
                for col_idx, cell in enumerate(row[:len(keys)]):
                    value = cell.value
                    if isinstance(value, bool):
                        if pos < len(df):
                            bool_found.setdefault(col_idx, {})[pos] = value
                        continue
                    if value is None:
                        continue
                    if isinstance(value, str):
                        _note_numeric_text(number_display, keys[col_idx], value)
                    elif isinstance(value, numbers.Real):
                        _note_display(number_display, keys[col_idx], float(value),
                                      fixed_decimals_from_format(cell.number_format))
        finally:
            wb.close()
    except Exception:
        return df, {}

    for col_idx, cells in bool_found.items():
        inferred = df.iloc[:, col_idx]
        if pd.api.types.is_bool_dtype(inferred) or not pd.api.types.is_numeric_dtype(inferred):
            continue                             # already boolean / already mixed: nothing was lost
        if any(pd.isna(inferred.iloc[pos]) or float(inferred.iloc[pos]) != float(value)
               for pos, value in cells.items()):
            continue                             # positions don't line up -- don't guess
        restored = inferred.astype(object)
        for pos, value in cells.items():
            restored.iloc[pos] = value
        df.isetitem(col_idx, restored)

    return df, number_display


def _text_number_display(path, header_row, encoding, sep, columns):
    """Same idea for CSV/TSV: pandas turns '4.30' into 4.3, so count decimals in the raw text."""
    store = {}
    try:
        raw = pd.read_csv(path, header=header_row, encoding=encoding, sep=sep,
                          dtype=str, keep_default_na=False)
        for col, key in zip(raw.columns, (normalize_name(c) for c in columns)):
            for text in raw[col].unique():
                _note_numeric_text(store, key, text)
    except Exception:
        return {}
    return store


# ---------------------------------------------------------------------------
# What pandas may silently turn into a blank while reading
# ---------------------------------------------------------------------------
# pandas quietly reads text like N/A, NA, n/a, NULL and None as an empty cell before
# this cleaner ever sees it -- the placeholder is gone with no trace in the log. Those
# words are disguised blanks (config.DISGUISED_BLANKS), which are kept and flagged PURPLE,
# not removed, so pandas must not blank them. Everything else on pandas' own list ("#N/A",
# "nan", ...) is still read as a blank exactly as before.
_PANDAS_DEFAULT_NA = (
    "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NaN", "-nan", "1.#IND",
    "1.#QNAN", "<NA>", "N/A", "NA", "NULL", "NaN", "None", "n/a", "nan", "null",
)


def _na_values_to_read():
    """The text pandas may still read as a blank: its own defaults minus our placeholders, plus ''."""
    keep = disguised_blank_values()
    return [""] + [v for v in _PANDAS_DEFAULT_NA if v.strip().lower() not in keep]



# ---------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------
def load_file(path):
    """Reads `path` (Excel or delimited text) into a LoadedFile."""
    ext = os.path.splitext(path)[1].lower()

    if ext in EXCEL_EXTENSIONS:
        xl, sheet_names = open_excel(path)
        sheet_name = sheet_names[0] if sheet_names else 0
        sheet_note = None
        try:
            if sheet_names:
                raw_df, sheet_name, sheet_note = _choose_sheet(xl, sheet_names)
            else:
                raw_df = xl.parse(sheet_name=sheet_name, header=None)
        except Exception as e:
            xl.close()
            raise RuntimeError(
                f"Could not parse sheet '{sheet_name}' of "
                f"'{os.path.basename(path)}': {e}"
            ) from e
        try:
            header_row = find_header_row(raw_df)
            original_df = xl.parse(sheet_name=sheet_name, header=header_row,
                                   keep_default_na=False, na_values=_na_values_to_read())
        finally:
            xl.close()
        number_display = {}
        if EXCEL_ENGINES.get(ext) == "openpyxl":
            original_df, number_display = _excel_extras_pass(path, sheet_name, header_row, original_df)
        return LoadedFile(path, ext, original_df, header_row,
                          sheet_name=sheet_name, sheet_names=sheet_names,
                          sheet_note=sheet_note, number_display=number_display)

    encoding = detect_text_encoding(path)
    # TSV must be read with a tab separator; otherwise every row collapses
    # into a single column. Other text extensions are sniffed.
    sep = "\t" if ext == ".tsv" else detect_separator(path, encoding)
    try:
        raw_df = pd.read_csv(path, header=None, encoding=encoding, sep=sep)
    except pd.errors.EmptyDataError as e:
        # BUG FIX: a genuinely empty (0-byte) file used to bubble up pandas'
        # own "No columns to parse from file" -- a confusing message that
        # doesn't say WHICH file or WHY, in a tool whose whole point is a
        # clear per-file pass/fail. run_batch() still catches this (one bad
        # file never stops the batch), but now the reason printed is plain.
        raise RuntimeError(
            f"'{os.path.basename(path)}' is empty -- nothing to clean"
        ) from e
    header_row = find_header_row(raw_df)
    original_df = pd.read_csv(path, header=header_row, encoding=encoding, sep=sep,
                              keep_default_na=False, na_values=_na_values_to_read())
    number_display = _text_number_display(path, header_row, encoding, sep, original_df.columns)
    return LoadedFile(path, ext, original_df, header_row, encoding=encoding,
                      number_display=number_display)


def _sha256_of(path, chunk_size=1 << 20):
    """
    SHA-256 of the raw source file, read in chunks so large workbooks don't
    get loaded into memory twice. Printed in the report so two logs can be
    matched to the exact bytes that were cleaned -- name/size/modified-time
    alone can't prove that (a re-saved copy can share all three).
    """
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_source(loaded):
    """
    The source-file facts the audit report prints (name, type, size, modified
    time, encoding, sheet). Returned as the initial `stats` dict.
    """
    path, ext = loaded.path, loaded.ext
    stats = {
        "filler_rows_top_removed": loaded.header_row,
        "source_file_name": os.path.basename(path),
        "file_type": FILE_TYPE_LABELS.get(ext.lstrip("."), ext.upper() or "Unknown"),
    }
    try:
        file_stat = os.stat(path)
        stats["source_file_size"] = file_stat.st_size
        stats["source_file_modified"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(file_stat.st_mtime)
        )
    except OSError:
        pass
    try:
        stats["source_file_sha256"] = _sha256_of(path)
    except OSError:
        pass
    if loaded.encoding is not None:
        stats["encoding"] = loaded.encoding
    if loaded.sheet_name is not None:
        stats["sheet_name"] = str(loaded.sheet_name)
        stats["sheet_names"] = [str(s) for s in (loaded.sheet_names or [])]
    if loaded.sheet_note:
        stats["sheet_note"] = loaded.sheet_note
    return stats
