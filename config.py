"""
All tweakable settings live here. Change behavior by editing this file --
you should never need to touch the other modules just to adjust a folder
path, a keyword list, or a casing rule.
"""

import os

# ---- Tool version ----
CLEANER_VERSION = "1.9.4"

# ---- Console feedback while a file is being cleaned ----
# Prints "  [ 4/23]  17%  trim_whitespace... done (0.31s)" for every stage of
# clean_file() as it runs, so the console never just sits on "Processing:
# <file>..." with no sign of life. Set to False to go back to silent/instant
# per-file output only.
SHOW_STEP_PROGRESS = True

# ---- Where files come from / go to ----
WATCH_DIR = r"C:\Users\koonera\OneDrive - Arriva\Documents\VSC\Cleaner\to_clean"
OUTPUT_ROOT = os.path.expanduser(r"~\Downloads")
EXTENSIONS = ["csv", "tsv", "txt", "xlsx", "xls", "xlsm", "xlsb"]

# ---- Excel extension -> pandas engine ----
EXCEL_ENGINES = {
    ".xlsx": "openpyxl",
    ".xlsm": "openpyxl",
    ".xls":  "xlrd",
    ".xlsb": "pyxlsb",
}

# ---- Disguised blanks: placeholder text that MEANS "nothing here" ----
DISGUISED_BLANKS = ["missing", "not available", "not applicable", "n/a", "na", "tbd", "unknown", "null", "none", " ", "", "-", "...", "???", ".", "?", "*"]

# ---- Column-name keywords used to decide a column's real type ----
ID_KEYWORDS = ["id", "code", "zip", "postcode", "sku", "no", "number", "app", "commodity_code", "ean"]
PRICE_KEYWORDS = ["price", "cost", "amount", "rate", "value", "total", "fee", "charge", "surcharge", "unit_price", "part_price",]
PRICE_COMPOUND_SUFFIXES = ["price", "cost", "amount", "total"]
NOT_PRICE_TOKENS = [
    "qty", "quantity", "quantities", "count", "items", "units", "pcs", "rows", "lines",
    "weight", "height", "length", "width", "depth", "volume",
    "code", "centre", "center", "type", "name", "date", "id", "no", "num", "number",
    "notes", "description", "status", "band",
    "exchange", "heart", "tax", "interest", "attribute", "default", "key", "field",
    "flag", "flags", "flagged",
    "percent", "percents", "percentage", "percentages", "pct",
]

# ---- Measurement columns (height, weight, quantity ...) ----
MEASURE_KEYWORDS = [
    "height", "weight", "length", "width", "depth", "diameter", "thickness",
    "volume", "dimension", "dimensions", "qty", "quantity",
]

# ---- App-number dedup (see steps/structure.dedupe_by_app_number) ----
APP_NUMBER_KEYWORDS = ["app_number", "app_num", "app_no", "app"]
DESCRIPTION_KEYWORDS = ["description", "part_description", "item_description"]

# ---- Unique-ID anomaly flagging (masks.flag_id_anomalies) ----
UNIQUE_ID_KEYWORDS = [
    "app_number", "app_num", "app_no", "app",
]

# ---- Flag columns (Y/N) -- see detectors.looks_like_boolean + steps/dtypes.py ----
FLAG_MIN_MATCH_RATE = 0.75

# ---- Casing rule applied when a column name contains the keyword ----
CASING_RULES = {
    "name": "title",
    "email": "lower",
    "country": "title",
    "status": "title",
}

# ---- Canonical value mappings, per column ----
CATEGORY_MAPPINGS = {
    "country": {
        "usa": "United States",
        "us": "United States",
        "u.s.a": "United States",
        "uk": "United Kingdom",
        "britain": "United Kingdom",
    },
    "status": {
        "active": "Active",
        "act": "Active",
        "inactive": "Inactive",
        "cancelled": "Inactive",
    },
}

# ---- Thresholds ----
HEADER_DETECTION_THRESHOLD = 0.5
DATE_DETECTION_THRESHOLD = 0.6
DUPLICATE_COLUMN_MATCH_THRESHOLD = 1.0

# ---- Phone-number detection (used by cleaning.format_phone_numbers) ----
PHONE_NAME_TOKENS = ["phone", "telephone", "tel", "mobile", "cell", "fax"]
PHONE_EXACT_NAMES = ["number", "phonenumber"]
PHONE_MIN_LIKE_RATE = 0.5
