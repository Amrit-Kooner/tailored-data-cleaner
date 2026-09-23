"""
Regression tests for bugs found during a bug-hunt pass on the loader and on
the pandas-3 "str" dtype migration:

1. A workbook whose first sheet is a cover/title sheet (one or two stray
   cells, no real table) used to always be read as-is, silently producing a
   completely empty result -- the real data on a later sheet was never even
   looked at, and nothing in the log or report said so.
2. pandas 3's default text dtype for CSV/Excel columns is "str", not
   "object". trim_whitespace() and fix_encoding_issues() used
   select_dtypes(include=["object"]) alone, which currently still matches
   "str" columns only via a documented-deprecated backward-compat fallback
   pandas warns will be removed -- at which point both steps would silently
   stop touching any text column read from a modern pandas file.
3. A genuinely empty (0-byte) source file used to bubble up pandas' own
   "No columns to parse from file", which doesn't name the offending file.

Run from the project root:
    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl
import pandas as pd

from uni_cleaner.pipeline import clean_file
from uni_cleaner.loaders import load_file


def _xlsx_with_sheets(sheets):
    """sheets: list of (name, rows) where rows is a list of row-lists."""
    path = os.path.join(tempfile.mkdtemp(), "wb.xlsx")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return path


class CoverSheetIsSkipped(unittest.TestCase):
    def test_blank_cover_sheet_falls_through_to_the_data_sheet(self):
        path = _xlsx_with_sheets([
            ("Cover", [["Company Confidential"]]),
            ("Data", [["id", "name", "price"], [1, "Widget", 9.99], [2, "Gadget", 19.99]]),
        ])
        loaded = load_file(path)
        self.assertEqual(loaded.sheet_name, "Data")
        self.assertIsNotNone(loaded.sheet_note)

        res = clean_file(path)
        self.assertEqual(res.df.shape[0], 2)
        self.assertIn("name", res.df.columns)
        self.assertEqual(sorted(res.df["name"].tolist()), ["Gadget", "Widget"])

    def test_normal_first_sheet_is_left_alone(self):
        path = _xlsx_with_sheets([
            ("Data", [["id", "name"], [1, "Widget"]]),
            ("Notes", [["ignore this sheet"]]),
        ])
        loaded = load_file(path)
        self.assertEqual(loaded.sheet_name, "Data")
        self.assertIsNone(loaded.sheet_note)

    def test_single_sheet_workbook_is_unaffected(self):
        path = _xlsx_with_sheets([
            ("Sheet1", [["id", "name"], [1, "Widget"], [2, "Gadget"]]),
        ])
        res = clean_file(path)
        self.assertEqual(res.df.shape, (2, 2))

    def test_every_sheet_blank_does_not_crash(self):
        path = _xlsx_with_sheets([("Cover", [[]]), ("Also Blank", [[]])])
        loaded = load_file(path)  # must not raise
        self.assertEqual(loaded.sheet_name, "Cover")


class TextStepsSurviveThePandas3StringDtype(unittest.TestCase):
    """
    pandas 3 reads CSV text columns as dtype "str" rather than "object".
    trim_whitespace and fix_encoding_issues must still find and clean them.
    """

    def test_trim_whitespace_cleans_str_dtype_columns(self):
        path = os.path.join(tempfile.mkdtemp(), "t.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("id,name\n1,\" Widget \"\n2,Gadget\n")
        df_check = pd.read_csv(path)
        self.assertEqual(str(df_check["name"].dtype), "str")  # confirms the scenario is real

        res = clean_file(path)
        self.assertEqual(res.df["name"].tolist(), ["Widget", "Gadget"])

    def test_no_pandas_deprecation_warning_from_select_dtypes(self):
        path = os.path.join(tempfile.mkdtemp(), "t2.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("id,name\n1,\" Widget \"\n2,Gadget\n")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            clean_file(path)
        select_dtype_warnings = [
            w for w in caught
            if "select_dtypes" in str(w.message) and "object" in str(w.message)
        ]
        self.assertEqual(select_dtype_warnings, [])


class EmptyFileGivesAClearError(unittest.TestCase):
    def test_zero_byte_file_names_itself_in_the_error(self):
        path = os.path.join(tempfile.mkdtemp(), "empty.csv")
        open(path, "w").close()
        with self.assertRaises(RuntimeError) as ctx:
            clean_file(path)
        self.assertIn("empty.csv", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
