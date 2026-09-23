"""
All tweakable settings live here. Change behavior by editing this file --
you should never need to touch the other modules just to adjust a folder
path, a keyword list, or a casing rule.
"""

import os

# ---- Where files come from / go to ----
WATCH_DIR = r"C:\Users\koonera\OneDrive - Arriva\Documents\VSC\Cleaner\to_clean"
OUTPUT_ROOT = os.path.expanduser(r"~\Downloads")
EXTENSIONS = ["csv", "tsv", "txt", "xlsx", "xls"]

# ---- Values treated as missing, even though they're not real NaN ----
DISGUISED_BLANKS = ["N/A", "n/a", "NA", "TBD", "unknown", "Unknown", "NULL", "None", ""]  # add more if needed....

# ---- Column-name keywords used to decide a column's real type ----
ID_KEYWORDS = ["id", "code", "zip", "postcode", "sku", "phone_number", "phone", "no", "number", "app"]  # add whatever needed.......
PRICE_KEYWORDS = ["price", "cost", "amount", "rate", "value", "total", "fee", "charge"]  # add whatever needed.......

# ---- Casing rule applied when a column name contains the keyword ----
CASING_RULES = {
    "name": "title",
    "email": "lower",
    "country": "title",
    "status": "title",
}  # add whatever needed......

# ---- Canonical value mappings, per column — fixes "USA"/"United States"/"usa" style
# variants that casing alone can't fix, since they're different words, not just casing.
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
}  # add whatever needed......

# ---- Thresholds ----
HEADER_DETECTION_THRESHOLD = 0.5
DATE_DETECTION_THRESHOLD = 0.6
DUPLICATE_COLUMN_MATCH_THRESHOLD = 1.0
